"""``paradise assets watch``, supervised by Blender while a document is open.

ONE watcher per project root, as a correctness rule: the engine's ``AssetWatcher.Drain`` drives
the sidecar maintainer outside its lock with an unsynchronized quarantine, and two drainers lose
the identity a move depends on. The watcher is a child (not detached like the runtime) so it can
be terminated; a SIGKILLed Blender leaves it running, which ``atexit`` and ``load_pre`` cannot
cover. Output goes to a log the panel reads the last error from, since a Blender-started watch
has no console (ParadiseEngine#192 is the tray that will replace this).
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
    "status_line",
    "stop",
    "stop_all",
    "watch_command",
    "watched_roots",
]

_WATCHERS: dict[str, subprocess.Popen] = {}

#: (exit code, last log line) per root, kept until the next start.
_EXITS: dict[str, tuple[int, str | None]] = {}


def log_path(project_root: str) -> str:
    """Per-root log path; two checkouts of one game must not share a log."""
    name = os.path.basename(os.path.normpath(project_root)) or "project"
    # hashlib, NOT hash(): str hashing is salted per process, so a later Blender would read a
    # path nothing wrote to and report no errors for a watcher reporting plenty.
    digest = hashlib.sha1(os.path.normcase(project_root).encode("utf-8")).hexdigest()[:6]
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


def watched_roots() -> list[str]:
    """Every project with a live watcher, for a panel or a diagnostic to report."""
    return [root for root in list(_WATCHERS) if is_running(root)]


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
    try:
        process = subprocess.Popen(  # argv is built from resolved paths
            argv,
            cwd=project_root,
            env=host.subprocess_environment(),
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
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
    block on it. On Windows ``terminate()`` is already a kill (#36)."""
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
    """Stop every watcher. Registered with ``atexit`` and called on ``load_pre``."""
    for root in list(_WATCHERS):
        stop(root)


def last_error(project_root: str) -> str | None:
    """The most recent failure line, or ``None``. Last, not first (unlike the play path):
    this log grows all session and the interesting rebuild is the latest."""
    path = log_path(project_root)
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-200:]
    except OSError:
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
    try:
        with open(log_path(project_root), encoding="utf-8", errors="replace") as handle:
            lines = [line.strip() for line in handle.readlines()[-40:] if line.strip()]
    except OSError:
        return None
    return lines[-1][:200] if lines else None


def exit_reason(project_root: str) -> str | None:
    """Why the watcher stopped, or ``None`` if it never started or is still running."""
    entry = _EXITS.get(_normalize(project_root))
    if entry is None:
        return None
    code, detail = entry
    return f"stopped (exit {code}): {detail}" if detail else f"stopped (exit {code})"


def status_line(project_root: str) -> str:
    """One line for a panel: whether it is watching, and the last thing that went wrong."""
    if not is_running(project_root):
        reason = exit_reason(project_root)
        return f"Watcher {reason}" if reason else "Not watching."
    problem = last_error(project_root)
    return f"Watching — last error: {problem}" if problem else "Watching."


def _on_load_pre(*_args) -> None:
    """``load_pre``, because after the load the old project is unreachable from the handler."""
    stop_all()


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
    finally:
        _adopting = False


def _on_load_post(*_args) -> None:
    """The new file is in; if it is a cached workfile, catch it up and watch its project."""
    adopt_loaded_file()


def register_handler() -> None:
    """Register the load handlers. ``@persistent`` or Blender drops them on the first file load,
    and watchers silently accumulate for the rest of the session."""
    import bpy

    handlers = (
        (_on_load_pre, bpy.app.handlers.load_pre),
        (_on_load_post, bpy.app.handlers.load_post),
    )
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
