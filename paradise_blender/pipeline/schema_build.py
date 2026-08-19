"""Rebuild the game project when its C# sources change, while Blender is open.

The authoring schema (``<data>/authoring-schema.json``) is dumped by BUILDING the game — a
Roslyn generator runs, a build target writes the file. The Godot host gets that for free from
its editor's Build button; Blender has no build button, so adding an ``[Authored]`` record used
to mean alt-tabbing to a terminal, running ``dotnet build``, and coming back. This watcher
closes that gap: a timer polls the project's source stamp, and when it changes (and settles —
half-written saves must not trigger half-built schemas), it runs ``dotnet build`` detached and
lets the Components panel's existing hot-reload pick up the fresh dump.

Never blocks the main thread: the build runs as a background process with its output in a temp
file, and the timer only ever polls ``returncode``. A failed build is reported once, with the
tail of its output, rather than silently leaving the dropdown stale — the symptom this module
exists to prevent.

``bpy`` is imported inside the functions that need it so the pure helpers (the source stamp,
the staleness test) stay importable under plain pytest.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import time

from .. import log

__all__ = [
    "dotnet_executable",
    "failure_summary",
    "is_schema_stale",
    "last_failure",
    "last_failure_log",
    "register",
    "source_stamp",
    "status_line",
    "unregister",
]

#: How long a changed source tree must hold still before a build starts. An IDE "save all"
#: writes files over tens of milliseconds; building mid-save would race the editor.
DEBOUNCE_SECONDS = 1.0

#: Poll cadence: fast enough that a rebuild starts about a second after saving, slow enough
#: that scanning ~100 file mtimes is invisible.
POLL_SECONDS = 1.0

_SKIPPED_DIRS = frozenset({"obj", "bin", ".git", ".vs", ".idea"})


def source_stamp(project_dir: str) -> tuple[int, int]:
    """A cheap change detector over the project's sources: (max mtime_ns, file count) of every
    ``.cs`` and ``.csproj`` under the directory, build output excluded. The count is in the
    stamp because deleting a file lowers no mtime."""
    newest = 0
    count = 0
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in _SKIPPED_DIRS]
        for name in files:
            if not name.endswith((".cs", ".csproj", ".props", ".targets")):
                continue
            try:
                stat = os.stat(os.path.join(root, name))
            except OSError:
                continue  # vanished mid-walk; the next tick sees the settled state
            newest = max(newest, stat.st_mtime_ns)
            count += 1
    return (newest, count)


def is_schema_stale(project_dir: str, schema_path: str) -> bool:
    """True when a source file is newer than the dumped schema — the state an author is in
    after editing C# with Blender closed, which a change-only watcher would never repair."""
    try:
        schema_mtime = os.stat(schema_path).st_mtime_ns
    except OSError:
        return True  # no dump at all; a build creates it
    newest, _count = source_stamp(project_dir)
    return newest > schema_mtime


def dotnet_executable() -> str | None:
    """Same resolution as the bridge: PATH first, then the places a GUI-launched Blender's
    minimal PATH tends to miss."""
    found = shutil.which("dotnet")
    if found:
        return found
    candidates = [
        "/usr/local/share/dotnet/dotnet",
        "/opt/homebrew/bin/dotnet",
        os.path.expanduser("~/.dotnet/dotnet"),
        r"C:\Program Files\dotnet\dotnet.exe",
    ]
    return next((c for c in candidates if os.path.exists(c)), None)


# --------------------------------------------------------------------------------------
# The watcher
# --------------------------------------------------------------------------------------

_timer_registered = False
_status = "off"
_failure_lines: list[str] = []
_failure_log_path: str | None = None
_last_stamp: tuple[int, int] | None = None
_pending_stamp: tuple[int, int] | None = None
_pending_since = 0.0
_build: subprocess.Popen | None = None
_build_log_path: str | None = None
_build_started = 0.0
_built_stamp: tuple[int, int] | None = None


def register() -> None:
    import bpy

    global _timer_registered
    if not _timer_registered:
        bpy.app.timers.register(_tick, first_interval=POLL_SECONDS, persistent=True)
        _timer_registered = True


def unregister() -> None:
    import bpy

    global _timer_registered
    if _timer_registered and bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    _timer_registered = False
    # A build in flight is left to finish: it is a detached OS process writing to the game's
    # own build output, and killing it halfway could leave a torn obj/ for the next build.


def failure_summary(output: str, limit: int = 10) -> list[str]:
    """The lines an author needs from a failed build's output: the compiler errors, deduped,
    in order — or the tail when nothing matches (a crash, a missing SDK). Pure, so the parsing
    that decides what a popup shows is pinned by unit tests rather than discovered live."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    errors: list[str] = []
    for line in lines:
        is_error = ": error " in line or line.startswith("error ") or "error MSB" in line
        if is_error and line not in errors:
            errors.append(line)
    return (errors or lines[-4:])[:limit]


def last_failure() -> list[str]:
    """The retained error lines of the most recent FAILED build; empty after a success."""
    return _failure_lines


def last_failure_log() -> str | None:
    """Path of the full compiler output of the last failed build, kept on disk until the next
    build starts or the failure clears."""
    return _failure_log_path


def status_line() -> str:
    """One human-readable line for the UI — a GUI-launched Blender never shows stdout, so the
    watcher's state has to be visible where the author is looking."""
    return _status


def _active_scene():
    """The scene to watch. Timer callbacks run with a RESTRICTED context — ``bpy.context.scene``
    is not guaranteed there — so fall back to the first open window's scene, which is where the
    author is working."""
    import bpy

    scene = getattr(bpy.context, "scene", None)
    if scene is not None:
        return scene
    for manager in bpy.data.window_managers:
        for window in manager.windows:
            return window.scene
    return next(iter(bpy.data.scenes), None)


def resolved_project(scene) -> str | None:
    """The watched .csproj as an absolute path, or None when the scene names none."""
    import bpy

    settings = getattr(scene, "paradise_project", None)
    raw = (settings.game_project if settings else "").strip()
    if not raw:
        return None
    path = os.path.abspath(bpy.path.abspath(raw))
    return path if os.path.isfile(path) else None


def start_build(project: str, reason: str) -> bool:
    """Kick off ``dotnet build`` in the background. False when one is already running or the
    dotnet CLI cannot be found (reported, not raised — this runs from a timer)."""
    global _build, _build_log_path, _build_started

    if _build is not None:
        return False
    dotnet = dotnet_executable()
    if dotnet is None:
        log.warn(
            "Cannot rebuild the game project: no `dotnet` CLI found on PATH or in the usual "
            "install locations. Build manually and the schema will hot-reload.")
        return False

    _discard_failure_log()
    handle, _build_log_path = tempfile.mkstemp(prefix="paradise_schema_build_", suffix=".log")
    _build = subprocess.Popen(
        [dotnet, "build", project],
        stdout=handle,
        stderr=subprocess.STDOUT,
        cwd=os.path.dirname(project),
    )
    os.close(handle)
    _build_started = time.monotonic()
    log.info(f"Rebuilding {os.path.basename(project)} ({reason})…")
    return True


def _finish_build() -> None:
    """Report a completed build and redraw, so the Components panel re-reads the schema the
    moment the dump lands rather than on the next mouse-over."""
    import bpy

    global _build, _build_log_path

    global _status, _failure_lines, _failure_log_path
    elapsed = time.monotonic() - _build_started
    failed = _build.returncode != 0
    if failed:
        tail = _read_tail(_build_log_path, lines=40)
        _failure_lines = failure_summary(tail)
        # The full output is KEPT: the summary is for the popup, but a template error or a
        # NuGet failure needs the whole log, and stdout is invisible to a Dock-launched Blender.
        _failure_log_path = _build_log_path
        _status = f"build FAILED ({elapsed:.1f}s)"
        log.warn(
            f"Game build FAILED ({elapsed:.1f}s) — the schema (and the Components dropdown) is "
            f"still the last successful build's. Full output: {_failure_log_path}\n"
            + "\n".join(_failure_lines))
        _announce_failure()
    else:
        _status = f"build ok ({elapsed:.1f}s)"
        _failure_lines = []
        _discard_failure_log()
        log.info(f"Game build finished ({elapsed:.1f}s); the authoring schema is fresh.")
        with contextlib.suppress(OSError, TypeError):
            os.unlink(_build_log_path)
    _build = None
    _build_log_path = None

    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            area.tag_redraw()


def _discard_failure_log() -> None:
    global _failure_log_path
    if _failure_log_path is not None:
        with contextlib.suppress(OSError):
            os.unlink(_failure_log_path)
    _failure_log_path = None


def _announce_failure() -> None:
    """A popup the moment the build fails. Best-effort: timers run without a window in
    headless sessions, and a failure must never take the watcher down with it — the panel
    alert (drawn from :func:`last_failure`) is the persistent fallback either way."""
    import bpy

    def draw(menu, _context):
        for line in _failure_lines[:8]:
            menu.layout.label(text=line[:130])
        menu.layout.separator()
        menu.layout.operator("paradise.show_build_errors", icon="COPYDOWN")

    with contextlib.suppress(Exception):
        bpy.context.window_manager.popup_menu(draw, title="Game build failed", icon="ERROR")


def _read_tail(path: str | None, lines: int = 12) -> str:
    if path is None:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            return "".join(file.readlines()[-lines:])
    except OSError:
        return "(build output unavailable)"


def _tick() -> float:
    """The registered timer. A timer that raises is silently unregistered by Blender — the
    watcher would die on its first hiccup and every later save would do nothing, which is the
    failure the author cannot see. So the real work is guarded, reported, and the timer lives."""
    global _status
    try:
        return _tick_guarded()
    except Exception as error:
        _status = f"watcher error: {error} (see console)"
        import traceback

        log.warn(f"Schema watcher tick failed; still watching. {traceback.format_exc()}")
        return 5.0


def _tick_guarded() -> float:
    global _last_stamp, _pending_stamp, _pending_since, _built_stamp, _status

    if _build is not None:
        if _build.poll() is None:
            _status = "building…"
            return 0.5
        _finish_build()
        return POLL_SECONDS

    scene = _active_scene()
    settings = getattr(scene, "paradise_project", None) if scene else None
    if settings is None or not settings.watch_game_project:
        _status = "off"
        return 2.0
    project = resolved_project(scene)
    if project is None:
        _status = (
            "no project — set Game Project to a .csproj" if not settings.game_project.strip()
            else f"Game Project not found: {settings.game_project}")
        return 2.0
    _status = f"watching {os.path.basename(project)}"

    project_dir = os.path.dirname(project)
    stamp = source_stamp(project_dir)

    if _last_stamp is None:
        # First look at this project. Do not treat existing files as "a change" — but DO
        # repair the case the watcher cannot otherwise see: sources edited while Blender was
        # closed, leaving the dump stale.
        _last_stamp = stamp
        _built_stamp = stamp
        from ..contract import authoring
        from ..prefs import resolve_blender_data_dir

        schema = authoring.schema_path(resolve_blender_data_dir(scene))
        if is_schema_stale(project_dir, schema):
            start_build(project, "the schema is older than the sources")
        return POLL_SECONDS

    if stamp != _last_stamp:
        _last_stamp = stamp
        _pending_stamp = stamp
        _pending_since = time.monotonic()
        return POLL_SECONDS

    if (_pending_stamp is not None
            and _pending_stamp != _built_stamp
            and time.monotonic() - _pending_since >= DEBOUNCE_SECONDS):
        if start_build(project, "sources changed"):
            _built_stamp = _pending_stamp
        _pending_stamp = None

    return POLL_SECONDS
