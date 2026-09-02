"""Rebuild the game when its C# changes, so the dumped schema stays fresh without a terminal.

A timer polls the source stamp and, once it settles (a half-written save must not build a
half-built schema), runs ``dotnet build`` detached and lets the panel's hot-reload pick up the
dump. Never blocks the main thread. ``bpy`` is imported inside functions so the pure helpers
stay importable under pytest. The build runs in :func:`.dotnet.subprocess_environment`
``play/host.py`` applies, so it fails in a Dock-launched macOS Blender.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import tempfile
import time

from .. import log
from . import dotnet

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


#: A guard against a cycle in a hand-edited csproj, not a real limit.
MAX_REFERENCE_DEPTH = 8

_PROJECT_REFERENCE = re.compile(
    r"""<ProjectReference\s[^>]*\bInclude\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def watched_dirs(project: str, _depth: int = 0) -> list[str]:
    """The project's directory plus those of its ProjectReferences, recursively: the
    ``[Authored]`` records live in libraries under the launcher that dumps them, so stamping the
    launcher alone never sees a new component. Read from csproj text (a timer must not shell
    out); a reference hidden behind an MSBuild property costs a manual rebuild, not a wrong schema."""
    directory = os.path.dirname(os.path.abspath(project))
    found = [directory]
    if _depth >= MAX_REFERENCE_DEPTH:
        return found
    try:
        with open(project, encoding="utf-8") as file:
            text = file.read()
    except OSError:
        return found
    for include in _PROJECT_REFERENCE.findall(text):
        referenced = os.path.normpath(
            os.path.join(directory, include.replace("\\", os.sep)))
        if not os.path.isfile(referenced):
            continue
        for nested in watched_dirs(referenced, _depth + 1):
            if nested not in found:
                found.append(nested)
    return found


def source_stamp(project_dir: str | list[str]) -> tuple[int, int]:
    """(max mtime_ns, file count) over ``.cs``/``.csproj``; the count is there because deleting
    a file lowers no mtime."""
    roots = [project_dir] if isinstance(project_dir, str) else project_dir
    newest = 0
    count = 0
    for root_dir in roots:
        for root, dirs, files in os.walk(root_dir):
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


def is_schema_stale(project_dir: str | list[str], schema_path: str) -> bool:
    """A source newer than the dump: the state after editing with Blender closed, which a
    change-only watcher would never repair."""
    try:
        schema_mtime = os.stat(schema_path).st_mtime_ns
    except OSError:
        return True  # no dump at all; a build creates it
    newest, _count = source_stamp(project_dir)
    return newest > schema_mtime


def dotnet_executable() -> str | None:
    return dotnet.executable()


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
    # Never kill a build in flight: a torn obj/ breaks the next build.


def failure_summary(output: str, limit: int = 10) -> list[str]:
    """The compiler errors of a failed build, deduped, or the tail when nothing matches."""
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
    """Full compiler output of the last failed build, kept until the next build starts."""
    return _failure_log_path


def status_line() -> str:
    """One line for the UI; a GUI-launched Blender never shows stdout."""
    return _status


def _active_scene():
    """The scene to watch; ``bpy.context.scene`` is not guaranteed in a timer's restricted context."""
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
    """Start ``dotnet build`` detached; False when one is running or dotnet is missing
    (reported, not raised: this runs from a timer)."""
    global _build, _build_log_path, _build_started

    if _build is not None:
        return False
    executable = dotnet.executable()
    if executable is None:
        log.warn(
            "Cannot rebuild the game project: no `dotnet` CLI found on PATH or in the usual "
            "install locations. Build manually and the schema will hot-reload.")
        return False

    _discard_failure_log()
    handle, _build_log_path = tempfile.mkstemp(prefix="paradise_schema_build_", suffix=".log")
    _build = subprocess.Popen(
        [executable, "build", project],
        stdout=handle,
        stderr=subprocess.STDOUT,
        cwd=os.path.dirname(project),
        env=dotnet.subprocess_environment(),
    )
    os.close(handle)
    _build_started = time.monotonic()
    log.info(f"Rebuilding {os.path.basename(project)} ({reason})…")
    return True


def _finish_build() -> None:
    """Report a completed build and redraw so the panel re-reads the dump now."""
    import bpy

    global _build, _build_log_path

    global _status, _failure_lines, _failure_log_path
    elapsed = time.monotonic() - _build_started
    failed = _build.returncode != 0
    if failed:
        tail = _read_tail(_build_log_path, lines=40)
        _failure_lines = failure_summary(tail)
        # The full log is kept: a NuGet failure needs it, and stdout is invisible from the Dock.
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
    """Best-effort popup: a headless timer has no window, and a failure here must never take
    the watcher down."""
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
    """The timer. Blender silently unregisters a timer that raises, so the work is guarded."""
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

    project_dir = watched_dirs(project)
    stamp = source_stamp(project_dir)

    if _last_stamp is None:
        # First look: existing files are not a change, but a dump older than the sources is.
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
