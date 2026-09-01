"""``paradise assets watch``, supervised by Blender for as long as a document is open.

Opening a level and having edits appear in the build should not also require remembering to start
a watcher in a terminal. The addon knows which project the open document belongs to, so it can
start one and stop it again.

**ONE WATCHER PER PROJECT, and that is a correctness rule rather than tidiness.** The engine's
``AssetWatcher.Drain`` documents it: the watcher's gate guards its event maps, but the sidecar
maintainer is driven OUTSIDE that lock (its work is filesystem IO, and holding a lock across it
would stall every incoming event) and its quarantine is unsynchronized. Two drainers race it, and
what they lose is the identity a move depends on -- a renamed asset gets a fresh guid and every
reference to it dangles. So :func:`start` is idempotent per project root, and two documents from
one project share the watcher rather than getting one each.

**Not detached, unlike the runtime.** ``play`` launches a game the author interacts with and
Blender has no business owning; a watcher is infrastructure for the session, so it is a child this
process can terminate. The cost is that a Blender killed outright (SIGKILL, a crash) leaves it
running -- :func:`stop_all` is registered with ``atexit`` and on ``load_pre``, which covers
quitting and opening another file, and nothing can cover the rest. A stray watcher is visible in a
task list and harmless beyond a rebuild nobody asked for, so the trade is stated rather than
solved.

**Output goes to a file, and the panel reads the last error out of it.** A watch started from
Blender has no console anyone is looking at, which is the whole reason a failed rebuild needs
somewhere to surface. That is also the case ParadiseEngine#192 (a tray icon) exists for; until it
lands, the panel is the only place an author finds out.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import subprocess
import tempfile

__all__ = [
    "is_running",
    "last_error",
    "log_path",
    "start",
    "status_line",
    "stop",
    "stop_all",
    "watched_roots",
]

#: Live watchers, by absolute project root. At most one per root -- see the module docstring.
_WATCHERS: dict[str, subprocess.Popen] = {}

#: Why a watcher stopped, by project root: (exit code, last thing its log said). Kept only until
#: the next start, so it describes the most recent failure rather than accumulating history.
_EXITS: dict[str, tuple[int, str | None]] = {}


def log_path(project_root: str) -> str:
    """Where one project's watcher writes.

    Keyed on the root's basename plus a hash of the whole path: two checkouts of the same game
    are a normal thing to have open, and a shared log would interleave them into nonsense.
    """
    name = os.path.basename(os.path.normpath(project_root)) or "project"
    # hashlib, NOT hash(): Python salts str hashing per process, so hash() would name a different
    # file on every Blender launch -- and the panel, running in a later process than the one that
    # started the watcher, would read a path nothing had ever written to and report no errors for
    # a watcher that was reporting plenty. Found by testing this end to end rather than by
    # reading it, which is the only way that class of bug shows up.
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
        # Reaped here rather than in a timer: nothing polls on a schedule, and a dead entry left
        # in the table would make `start` a no-op forever after the first crash.
        #
        # WHY IT DIED IS REMEMBERED. A watcher that starts and then stops used to leave the panel
        # saying "Not watching" -- identical to never having started one, and impossible to act
        # on. The exit code and whatever the log last said are kept so the panel can say what
        # happened instead of what is no longer true.
        _WATCHERS.pop(key, None)
        _EXITS[key] = (code, last_error(project_root) or _last_line(project_root))
        return False

    _EXITS.pop(key, None)
    return True


def watched_roots() -> list[str]:
    """Every project with a live watcher, for a panel or a diagnostic to report."""
    return [root for root in list(_WATCHERS) if is_running(root)]


def start(project_root: str) -> str | None:
    """Start this project's watcher, or return why it could not.

    Returns ``None`` both when one was started and when one was ALREADY running -- "there is a
    watcher for this project" is the postcondition a caller wants, and the two ways of reaching
    it are not worth distinguishing at the call site.
    """
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

    path = log_path(project_root)
    try:
        handle = open(path, "w", encoding="utf-8")
    except OSError as error:
        return f"Could not open the watch log: {error}"

    argv = [*command, "assets", "watch", "--project", project_root]
    try:
        process = subprocess.Popen(  # argv is built from resolved paths
            argv,
            cwd=project_root,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as error:
        handle.close()
        return f"Could not start the watcher: {error}"
    finally:
        # The child holds its own duplicate of the descriptor, so this process does not need to
        # keep one open -- and on Windows an open handle here would lock the file against the
        # next run's truncate.
        handle.close()

    _WATCHERS[key] = process
    return None


def start_for(project_root: str) -> str | None:
    """Start a watcher if the author has not turned the behaviour off.

    Separate from :func:`start` so the preference is consulted in exactly one place, and so the
    panel's own button can start one unconditionally -- an author who turned the automatic
    behaviour off may still want one for this session, and a button that silently did nothing
    would be the worst of both.
    """
    from . import prefs

    preferences = prefs.get_preferences()
    if preferences is not None and not preferences.auto_watch:
        return None
    return start(project_root)


def stop(project_root: str) -> None:
    """Stop this project's watcher, if it has one.

    Terminate then kill: the CLI handles the interrupt to put the watcher down rather than being
    shot mid-write, and a short wait is what lets it. A watcher that ignores both is killed,
    because Blender must not block on it.
    """
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
        # Already gone. Nothing to do, and nothing worth reporting: the postcondition holds.
        pass


def stop_all() -> None:
    """Stop every watcher. Registered with ``atexit`` and called on ``load_pre``."""
    for root in list(_WATCHERS):
        stop(root)


def last_error(project_root: str) -> str | None:
    """The most recent line of the log that looks like a failure, or ``None``.

    The LAST rather than the first, which is the opposite of what the play path wants: a build log
    is read once after a run that already failed, while this file grows for as long as the session
    does and the interesting rebuild is the one that just happened.
    """
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

        # A failed rebuild ends with a COUNT -- "watch: build FAILED with 1 error(s)" -- and the
        # line that says WHAT went wrong is above it. Taking the last match verbatim would show an
        # author the tally and hide the sentence naming the file, which is the one thing they need
        # to act. So the summary is remembered and the scan keeps going for a detail line.
        if _is_summary(lowered):
            summary = summary or text
            continue
        return text[:200]

    return summary[:200] if summary is not None else None


def _is_summary(lowered: str) -> bool:
    """Whether a line is a rebuild's tally rather than a description of what failed."""
    return "build failed with" in lowered


def _last_line(project_root: str) -> str | None:
    """The final non-empty line of the log, whatever it says.

    The fallback when a watcher exits without anything that looks like an error: a process that
    stopped for a reason it did not phrase as one still left its last words, and those are more
    use than an exit code alone.
    """
    try:
        with open(log_path(project_root), encoding="utf-8", errors="replace") as handle:
            lines = [line.strip() for line in handle.readlines()[-40:] if line.strip()]
    except OSError:
        return None
    return lines[-1][:200] if lines else None


def exit_reason(project_root: str) -> str | None:
    """Why this project's watcher stopped, or ``None`` if it never started or is still running.

    Exists because "Not watching" is what a panel showed for BOTH "you never started one" and
    "the one you started died a second later", and an author cannot tell those apart or act on
    either.
    """
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
    """Blender is about to replace the session, so this document's watcher is finished.

    ``load_pre`` and not ``load_post``: after the load the scene is the NEW file's, and the
    project the watcher belongs to is no longer reachable from anything the handler can see.
    """
    stop_all()


def register_handler() -> None:
    """Stop watchers when Blender opens another file.

    Persistent, for the reason every handler in these addons is: without it Blender drops the
    handler on the first file load, and the failure is invisible -- watchers simply start
    accumulating, one per document anyone opens for the rest of the session.
    """
    import bpy

    _on_load_pre.__dict__.setdefault("_bpy_persistent", True)
    handler = bpy.app.handlers.persistent(_on_load_pre)
    globals()["_HANDLER"] = handler
    if handler not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(handler)


def unregister_handler() -> None:
    import bpy

    handler = globals().get("_HANDLER")
    if handler is not None and handler in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(handler)
    globals()["_HANDLER"] = None
    # Disabling the addon should not leave a watcher behind: nothing would be left that knows how
    # to stop it, and the atexit hook goes with the module on a reload.
    stop_all()


#: Quitting Blender is the case ``load_pre`` cannot see. It does not cover a crash or a SIGKILL,
#: which is stated in the module docstring rather than pretended away.
atexit.register(stop_all)
