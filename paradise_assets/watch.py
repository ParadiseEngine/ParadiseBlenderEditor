"""``paradise assets watch``, supervised by Blender while a document is open.

ONE watcher per project root, as a correctness rule: the engine's ``AssetWatcher.Drain`` drives
the sidecar maintainer outside its lock with an unsynchronized quarantine, and two drainers lose
the identity a move depends on. The watcher is a child (not detached like the runtime) so it can
be terminated; a SIGKILLed Blender leaves it running, which ``atexit`` cannot cover. Output
goes to a log the panel reads the last error from, since a Blender-started watch has no
console (ParadiseEngine#192 is the tray that will replace this).
"""

from __future__ import annotations

import atexit
import hashlib
import os
import subprocess
import tempfile

__all__ = [
    "adopt_loaded_file",
    "is_running",
    "last_error",
    "log_path",
    "start",
    "stop",
    "stop_all",
    "watch_command",
]

_WATCHERS: dict[str, subprocess.Popen] = {}

#: (exit code, last log line) per root, kept until the next start.
_EXITS: dict[str, tuple[int, str | None]] = {}


def log_path(project_root: str) -> str:
    """Per-root log path; two checkouts of one game must not share a log."""
    name = os.path.basename(os.path.normpath(project_root)) or "project"
    # hashlib, NOT hash(): str hashing is salted per process, so a later Blender would read a
    # path nothing wrote to and report no errors for a watcher reporting plenty. Normalised the
    # same way as the watcher table, so a relative spelling finds the log an absolute one wrote.
    digest = hashlib.sha1(_normalize(project_root).encode("utf-8")).hexdigest()[:6]
    return os.path.join(tempfile.gettempdir(), f"paradise_assets_watch_{name}_{digest}.log")


def _normalize(project_root: str) -> str:
    return os.path.normcase(os.path.abspath(project_root))


def is_running(project_root: str) -> bool:
    """Whether this project's watcher is alive, reaping it if it has exited."""
    key = _normalize(project_root)
    process = _WATCHERS.get(key)
    if process is None:
        return False
    if (code := process.poll()) is not None:
        # A dead entry left in the table would make `start` a no-op forever after a crash; the
        # exit reason is kept so the panel can say more than "Not watching".
        _WATCHERS.pop(key, None)
        _EXITS[key] = (code, last_error(project_root) or _last_line(project_root))
        return False

    _EXITS.pop(key, None)
    return True


def watch_command(cli_argv: list[str], project_root: str) -> list[str]:
    """The watch argv; ``--editor`` so play mode starts on (the tray can turn it off)."""
    from .play import host

    profile = host._preference("build_profile", "dev") or "dev"
    return [*cli_argv, "assets", "watch", "--editor", "--profile", profile, "--project", project_root]


def start(project_root: str) -> str | None:
    """Ensure this project has a watcher; returns why it could not, or ``None``."""
    from .play import host

    key = _normalize(project_root)
    if is_running(key):
        return None

    command = host.resolve_cli_command()
    if command is None:
        return (
            "No `paradise` CLI found. Install it as a dotnet tool, or point 'Paradise CLI' in "
            "the addon preferences at it."
        )

    problem = host.ensure_cli_built()
    if problem:
        return problem

    path = log_path(project_root)
    try:
        handle = open(path, "w", encoding="utf-8")  # noqa: SIM115 -- handed to the child, then closed
    except OSError as error:
        return f"Could not open the watch log: {error}"

    argv = watch_command(command, project_root)
    # No console window per watcher on Windows; a GUI Blender would otherwise pop one.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        process = subprocess.Popen(  # argv is built from resolved paths
            argv,
            cwd=project_root,
            env=host.subprocess_environment(),
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError) as error:
        handle.close()
        return f"Could not start the watcher: {error}"
    finally:
        # On Windows an open handle here would lock the log against the next run's truncate.
        handle.close()

    _WATCHERS[key] = process
    return None


def start_for(project_root: str) -> str | None:
    """Start a watcher unless the preference is off; the panel button bypasses this."""
    from . import prefs

    preferences = prefs.get_preferences()
    if preferences is not None and not preferences.auto_watch:
        return None
    return start(project_root)


def stop(project_root: str) -> None:
    """Terminate, wait briefly so the CLI can finish a write, then kill; Blender must not
    block on it. On POSIX the CLI handles SIGTERM and finishes its write; on Windows
    ``terminate()`` IS ``TerminateProcess``, so the grace period only covers a write already in
    the kernel."""
    key = _normalize(project_root)
    process = _WATCHERS.pop(key, None)
    if process is None or process.poll() is not None:
        return

    try:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
    except OSError:
        pass


def stop_all() -> None:
    """Stop every watcher. Registered with ``atexit`` and called on unregister."""
    for root in list(_WATCHERS):
        stop(root)


#: log path -> ((mtime_ns, size), answer). The panel asks per redraw; the log only matters
#: when it grew.
_LAST_ERRORS: dict[str, tuple[tuple[int, int], str | None]] = {}

#: Enough for the last rebuild's diagnostics; the tally line and the file naming the error are
#: within a few hundred lines of the end.
_TAIL_BYTES = 64 * 1024


def _tail_lines(path: str, count: int) -> list[str] | None:
    """The last *count* lines, reading only the file's tail; ``None`` when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    return tail.splitlines()[-count:]


def last_error(project_root: str) -> str | None:
    """The most recent failure line, or ``None``. Last, not first (unlike the play path):
    this log grows all session and the interesting rebuild is the latest."""
    path = log_path(project_root)
    try:
        stat = os.stat(path)
    except OSError:
        return None
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _LAST_ERRORS.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    answer = _scan_for_error(path)
    _LAST_ERRORS[path] = (stamp, answer)
    return answer


def _scan_for_error(path: str) -> str | None:
    lines = _tail_lines(path, 200)
    if lines is None:
        return None

    summary = None
    for line in reversed(lines):
        text = line.strip()
        if not text:
            continue
        lowered = text.lower()
        if not (lowered.startswith("error") or "failed" in lowered):
            continue

        # The tally line ("build FAILED with 1 error(s)") comes last; the line naming the file
        # is above it, so keep scanning past the tally.
        if _is_summary(lowered):
            summary = summary or text
            continue
        return text[:200]

    return summary[:200] if summary is not None else None


def _is_summary(lowered: str) -> bool:
    """Whether a line is a rebuild's tally rather than a description of what failed."""
    return "build failed with" in lowered


def _last_line(project_root: str) -> str | None:
    """The final non-empty log line, for a watcher that exited without a recognisable error."""
    tail = _tail_lines(log_path(project_root), 40)
    if tail is None:
        return None
    lines = [line.strip() for line in tail if line.strip()]
    return lines[-1][:200] if lines else None


def exit_reason(project_root: str) -> str | None:
    """Why the watcher stopped, or ``None`` if it never started or is still running."""
    entry = _EXITS.get(_normalize(project_root))
    if entry is None:
        return None
    code, detail = entry
    return f"stopped (exit {code}): {detail}" if detail else f"stopped (exit {code})"


#: Nested adopt is a real case -- rematerialize imports GLBs, and an importer that loaded a
#: file would re-enter ``load_post``. The objects would be rebuilt twice and a second watcher
#: start would race the first. The flag is the whole defence; do not "simplify" it away.
_adopting = False


def adopt_loaded_file(*_args) -> None:
    """Refresh every document-backed scene from ``assets/`` and start its watcher. Also called
    from register: enabling the addon onto an open workfile does not fire ``load_post``."""
    global _adopting
    if _adopting:
        return

    import bpy

    # Startup registers addons against a restricted ``bpy.data`` that carries no collections.
    # Raising here would abort register() before the rest of the addon is wired up, and the file
    # being opened fires ``load_post`` once the real data is in, so skipping costs nothing.
    if not hasattr(bpy.data, "scenes"):
        return

    from .document import project as project_layout
    from .materialize import store, workfile

    _adopting = True
    try:
        seen: set[str] = set()
        for scene in bpy.data.scenes:
            problem = workfile.refresh_from_document(scene)
            if problem:
                print(f"[paradise_assets] could not refresh from assets: {problem}")

            state = store.read_state(scene)
            if state is None:
                continue
            located = project_layout.locate(state.path)
            if located is None:
                continue
            if located.root in seen:
                continue
            seen.add(located.root)
            watch_problem = start_for(located.root)
            if watch_problem:
                print(f"[paradise_assets] {watch_problem}")

        # The old file's projects, no longer open, lose their watcher; a shared one survives.
        wanted = {_normalize(root) for root in seen}
        for root in list(_WATCHERS):
            if root not in wanted:
                stop(root)
    finally:
        _adopting = False


def _on_load_post(*_args) -> None:
    """The new file is in; if it is a cached workfile, catch it up and watch its project."""
    adopt_loaded_file()


def register_handler() -> None:
    """Register the load handler. ``@persistent`` or Blender drops it on the first file load,
    and watchers silently accumulate for the rest of the session."""
    import bpy

    handlers = ((_on_load_post, bpy.app.handlers.load_post),)
    stored: list = []
    for function, collection in handlers:
        function.__dict__.setdefault("_bpy_persistent", True)
        handler = bpy.app.handlers.persistent(function)
        stored.append((handler, collection))
        if handler not in collection:
            collection.append(handler)
    globals()["_HANDLERS"] = stored

    # Enabling the addon with a workfile already open does not fire load_post.
    adopt_loaded_file()


def unregister_handler() -> None:
    for handler, collection in globals().get("_HANDLERS") or ():
        if handler in collection:
            collection.remove(handler)
    globals()["_HANDLERS"] = []
    # After a disable nothing is left that knows how to stop a watcher.
    stop_all()


#: Quitting is the case ``load_pre`` cannot see; a crash or SIGKILL is covered by nothing.
atexit.register(stop_all)


def ensure(project_root: str) -> str | None:
    """Make sure the project has a watcher to mint sidecars, or say why it cannot have one."""
    if is_running(project_root):
        return None
    problem = start(project_root)
    if problem is None:
        return None
    return (
        f"{problem} The asset watcher is what gives a new prefab its identity, so nothing can "
        "be created without it."
    )
