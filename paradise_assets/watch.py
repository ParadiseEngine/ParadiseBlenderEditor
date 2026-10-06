"""Supervises one ``paradise assets watch`` per project and reads back what it reported.

ONE watcher per project root, as a correctness rule: the engine's ``AssetWatcher.Drain`` drives
the sidecar maintainer outside its lock with an unsynchronized quarantine, and two drainers lose
the identity a move depends on. POSIX watchers drain on SIGINT; unconfirmed termination
blocks builds rather than risking orphaned child writers. New watchers have process-tree
ownership under a separate key from Play (a kill-on-close Job on Windows). Independent
external watchers are outside this supervisor. Output goes to a per-project log the panel reads.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field

from .play import process_tree

__all__ = [
    "Rebuild",
    "WatchPause",
    "adopt_loaded_file",
    "is_paused",
    "is_running",
    "last_error",
    "last_rebuild",
    "log_path",
    "prepare_pause",
    "reserve_pause",
    "start",
    "stop",
    "stop_all",
    "watch_command",
]

_WATCHERS: dict[str, subprocess.Popen] = {}
_OWNERSHIPS: dict[str, process_tree.Ownership] = {}

#: (exit code, last log line) per root, kept until the next start.
_EXITS: dict[str, tuple[int, str | None]] = {}

# The registry lock is never held while waiting for a project. A slow watcher must not
# prevent a different project's watcher from starting or stopping.
_REGISTRY_LOCK = threading.RLock()
_ENABLED = True


@dataclass(frozen=True)
class _Launch:
    root: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]


@dataclass
class _Project:
    lock: threading.RLock = field(default_factory=threading.RLock)
    pauses: set[WatchPause] = field(default_factory=set)
    owner: WatchPause | None = None
    pending: _Launch | None = None
    disabled: bool = False
    starting: bool = False
    revision: int = 0
    error: str | None = None
    unsafe: str | None = None


_PROJECTS: dict[str, _Project] = {}


def _project(key: str) -> _Project:
    with _REGISTRY_LOCK:
        return _PROJECTS.setdefault(key, _Project())


def _roots() -> set[str]:
    with _REGISTRY_LOCK:
        return set(_PROJECTS) | set(_WATCHERS) | set(_OWNERSHIPS)


def _main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def _launch(
    root: str, cli_argv: list[str], profile: str, environment: dict[str, str],
) -> _Launch:
    return _Launch(
        root, tuple(watch_command(cli_argv, root, profile=profile)),
        tuple(environment.items()),
    )


class WatchPause:
    """A main-thread reservation, released in the build worker's ``finally`` block.

    ``pause()`` must succeed before any asset/launcher writer starts. Another reservation
    owning the build slot returns an error, not permission to build concurrently. ``resume()``
    is idempotent, including when cancellation happened before ``pause()``. Neither method
    consults Blender, preferences, or host resolution. Release only after build children exit.
    """

    def __init__(self, root: str, state: _Project):
        self.root = root
        self._state = state
        self._released = False
        self._cancelled = False

    def pause(self) -> str | None:
        """Confirm watcher/tree exit and exclusively claim this project's build slot."""
        state = self._state
        with state.lock:
            if self._released:
                return "This watcher pause has already been released; reserve a new pause."
            if not _ENABLED:
                return "Watcher supervision stopped; cancel this build and re-enable the addon."
            if self._cancelled:
                return "This watcher pause was cancelled by Stop All; reserve a new pause."
            if state.owner is not None and state.owner is not self:
                return "Another build owns this project's watcher pause; finish or cancel it first."
            if problem := _stop_process(self.root, state):
                return problem
            state.owner = self
            return None

    def resume(self) -> str | None:
        """Release the reservation, restoring at most one watcher after the last release."""
        state = self._state
        with state.lock:
            if self._released:
                return None
            self._released = True
            state.pauses.discard(self)
            if state.owner is self:
                state.owner = None
            if state.pauses:
                return None
            request, state.pending = state.pending, None
            if not _ENABLED or state.disabled or request is None:
                return None
            return _spawn(request, state)


def prepare_pause(
    project_root: str, resolved_cli_command: list[str], prepared_env: dict[str, str],
) -> WatchPause:
    """Capture the profile and reserve before dispatching Play's build worker.

    The caller already resolved the CLI and environment. In the worker, check ``pause()``
    before building, then check ``resume()`` before host play and call it again in ``finally``.
    A non-``None`` result is a terminal build/restoration error, not permission to proceed.
    """
    if not _main_thread():
        raise RuntimeError("Prepare the watcher pause on the main thread.")
    from .play import host

    profile = host._preference("build_profile", "dev") or "dev"
    return reserve_pause(
        project_root, resolved_cli_command, profile=profile, environment=prepared_env,
    )


def reserve_pause(
    project_root: str, cli_argv: list[str], *, profile: str, environment: dict[str, str],
) -> WatchPause:
    """Reserve on the main thread before dispatching a build worker.

    CLI argv, profile and environment must already be resolved on the main thread. They
    are copied, not read again on restoration. The latest reservation/start settings win,
    but merely reserving an inactive or manually stopped watcher never enables it.
    All reservations, even unused/failed ones, require ``resume()`` in cleanup.
    """
    if not _main_thread():
        raise RuntimeError("Reserve the watcher pause on the main thread before starting the worker.")
    key = _normalize(project_root)
    request = _launch(key, cli_argv, profile, environment)
    state = _project(key)
    with state.lock:
        token = WatchPause(key, state)
        token._cancelled = not _ENABLED
        if _ENABLED and not state.disabled and (state.pending is not None or is_running(key)):
            state.pending = request
        state.pauses.add(token)
        return token


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


def is_paused(project_root: str) -> bool:
    """Whether a reservation is withholding this project's watcher, even before shutdown."""
    state = _project(_normalize(project_root))
    # A redraw must not wait for the worker's shutdown wait. Mutations remain serialized.
    return bool(state.pauses)


def is_running(project_root: str) -> bool:
    """Whether this project's watcher is alive, reaping it if it has exited."""
    state = _project(key := _normalize(project_root))
    with state.lock:
        process = _WATCHERS.get(key)
        if process is None:
            return False
        if (code := process.poll()) is not None:
            _WATCHERS.pop(key, None)
            _EXITS[key] = (code, last_error(key) or _last_line(key))
            if code != 0 and key not in _OWNERSHIPS:
                state.unsafe = _unsafe_exit(key)
            elif code == 0 and key not in _OWNERSHIPS and state.unsafe is None:
                state.error = None
            return False

        _EXITS.pop(key, None)
        return True


def watch_command(
    cli_argv: list[str], project_root: str, *, profile: str | None = None,
) -> list[str]:
    """The watch argv; supplying a captured profile makes this worker-safe."""
    if profile is None:
        if not _main_thread():
            raise RuntimeError("Capture the watcher profile on the main thread.")
        from .play import host

        profile = host._preference("build_profile", "dev") or "dev"
    return [*cli_argv, "assets", "watch", "--editor", "--profile", profile, "--project", project_root]


def start(project_root: str) -> str | None:
    """Start or defer an explicit start. Only ``ensure()`` promises identity readiness."""
    if not _main_thread():
        return "Request watcher starts on the main thread; use a captured WatchPause in workers."
    state = _project(key := _normalize(project_root))
    with state.lock:
        if not _ENABLED:
            return "Watcher supervision is stopped; re-enable the addon before starting a watcher."
        running = not state.pauses and is_running(key)
        if state.unsafe:
            return state.unsafe
        if running:
            return state.error
        if state.starting:
            return "A watcher start is already being prepared for this project; try again shortly."
        from .play import host

        revision = state.revision
        state.starting = True
        try:
            command = host.resolve_cli_command(key)
            if command is None:
                return (
                    "No `paradise` CLI found. Install it as a dotnet tool, or point 'Paradise CLI' "
                    "in the addon preferences at it."
                )
            # Play owns CLI compilation while reserved; a deferred start only captures intent.
            if not state.pauses and (problem := host.ensure_cli_built(key)):
                return problem
            request = _launch(
                key, command, host._preference("build_profile", "dev") or "dev",
                host.subprocess_environment(),
            )
            # Host resolution can re-enter Blender callbacks; a later Stop wins.
            if revision != state.revision or not _ENABLED:
                return "The watcher start was cancelled by Stop."
            state.disabled = False
            if state.pauses:
                state.pending = request
                return None
            return _spawn(request, state)
        finally:
            state.starting = False


def _spawn(request: _Launch, state: _Project) -> str | None:
    """Called with the project lock held and no outstanding pauses; no host/prefs calls."""
    if state.unsafe:
        return state.unsafe
    if is_running(request.root):
        # A timed-out SIGINT may still be draining a build, but will exit afterwards.
        # Do not drop restoration intent while claiming the old watcher was restored.
        return state.error
    if state.unsafe:
        return state.unsafe
    if (
        (_containment_available() or request.root in _OWNERSHIPS)
        and (problem := _stop_process(request.root, state))
    ):
        return problem
    try:
        handle = open(log_path(request.root), "w", encoding="utf-8")  # noqa: SIM115
    except OSError as error:
        state.error = f"Could not open the watch log: {error}"
        return state.error

    contained = _containment_available()
    try:
        options = (
            process_tree.launch_options(dict(request.environment)) if contained
            else {"env": dict(request.environment), "creationflags": 0}
        )
        process = subprocess.Popen(
            list(request.argv), cwd=request.root, stdout=handle,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **options,
        )
        _WATCHERS[request.root] = process
        if contained:
            try:
                _OWNERSHIPS[request.root] = process_tree.record(
                    _ownership_root(request.root), process, environment=options["env"],
                )
            except BaseException as error:
                problem = _failed_record(request.root, process, state, error)
                if not isinstance(error, (OSError, subprocess.SubprocessError)):
                    raise
                return problem
    except (OSError, subprocess.SubprocessError) as error:
        state.error = f"Could not start the watcher: {error}"
        return state.error
    finally:
        handle.close()
    _EXITS.pop(request.root, None)
    state.error = None
    return None


def start_for(project_root: str) -> str | None:
    """Start a watcher unless the preference is off; the panel button bypasses this."""
    if not _main_thread():
        return "Request automatic watcher starts on the main thread."
    from . import prefs

    preferences = prefs.get_preferences()
    if preferences is not None and not preferences.auto_watch:
        return None
    return start(project_root)


def _unsafe_exit(key: str) -> str:
    return (
        f"The watcher for {key} exited without graceful cleanup; child writers may remain. "
        "Close any remaining watcher child builds, then restart Blender before building again."
    )


def _containment_available() -> bool:
    return sys.platform in ("win32", "linux", "darwin")


def _uses_job() -> bool:
    return os.name == "nt"


def _ownership_root(key: str) -> str:
    # Never share the game's ownership record: stopping its launcher must not stop the watcher.
    return os.path.join(key, ".editor", "asset-watcher")


def _failed_record(
    key: str, process: subprocess.Popen, state: _Project, error: BaseException,
) -> str:
    if not _uses_job():
        # A POSIX child has already started and may have descendants. Killing only its
        # parent after failed ownership recording would silently leave those writers alive.
        state.unsafe = state.error = (
            f"Could not contain the asset watcher: {error}. Child writers may remain. "
            "Close this watcher and its child builds, then restart Blender before building again."
        )
        return state.error
    # Windows launch_options suspends the child until record assigns its Job. Only this
    # not-yet-started child is safe to kill directly; an already-running watcher is not.
    problems = []
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5.0)
        if process.poll() is None:
            problems.append("the suspended child did not exit")
    except (OSError, subprocess.SubprocessError) as cleanup_error:
        problems.append(str(cleanup_error))
    if problem := process_tree.stop(_ownership_root(key)):
        problems.append(problem)
    state.error = f"Could not contain the asset watcher: {error}"
    if problems:
        state.unsafe = state.error = (
            f"{state.error}; cannot confirm cleanup: {'; '.join(problems)}. "
            "Close remaining watcher processes and restart Blender before building again."
        )
    else:
        _WATCHERS.pop(key, None)
    return state.error


def _can_terminate_gracefully() -> bool:
    # Legacy Windows watchers have no Job; TerminateProcess cannot drain their children.
    return os.name != "nt"


def _interrupt_process(key: str, state: _Project, process: subprocess.Popen) -> str | None:
    if process.poll() is not None:
        return None
    detail = "no safe graceful termination is available on this platform"
    if _can_terminate_gracefully():
        try:
            # The CLI handles Console.CancelKeyPress, not SIGTERM, and drains its rebuild.
            process.send_signal(signal.SIGINT)
            process.wait(timeout=5.0)
            if process.poll() is not None:
                return None
            detail = "it is still running after the shutdown wait"
        except (OSError, subprocess.SubprocessError) as error:
            detail = str(error)
    state.error = (
        f"Cannot safely stop the asset watcher for {key}: {detail}. Build is blocked. "
        "Close this project's watcher from its tray/terminal, wait for its child builds "
        "to finish, then restart Asset Watch and retry. The watcher may still be stopping; "
        "it was not force-killed because child writers could survive."
    )
    return state.error


def _stop_process(key: str, state: _Project) -> str | None:
    """Keep ownership until exit is confirmed. Never force-kill an uncontained writer."""
    if state.unsafe:
        return state.unsafe
    process = _WATCHERS.get(key)
    ownership = _OWNERSHIPS.get(key)
    if ownership is not None:
        if (
            process is not None and not _uses_job()
            and (problem := _interrupt_process(key, state, process))
        ):
            return problem
        problem = process_tree.release(_ownership_root(key), ownership)
        if problem is None and process is not None and _uses_job():
            try:
                process.wait(timeout=5.0)
                if process.poll() is None:
                    problem = "the watcher is still running after releasing its process Job"
            except (OSError, subprocess.SubprocessError) as error:
                problem = str(error)
        if problem:
            state.error = (
                f"Cannot confirm the asset watcher for {key} stopped: {problem}. "
                "Build is blocked; retry Stop after resolving the process-tree cleanup error."
            )
            return state.error
        _OWNERSHIPS.pop(key, None)
        _WATCHERS.pop(key, None)
        _EXITS.pop(key, None)
        state.error = None
        return None
    if process is None:
        # Reclaim a prior Blender session's watcher tree, never the game's ownership key.
        if _containment_available() and (problem := process_tree.stop(_ownership_root(key))):
            state.error = f"Cannot reclaim the earlier asset watcher; build is blocked: {problem}"
            return state.error
        state.error = None
        _EXITS.pop(key, None)
        return None
    if problem := _interrupt_process(key, state, process):
        return problem
    if process.poll() != 0:
        state.unsafe = state.error = _unsafe_exit(key)
        return state.unsafe
    _WATCHERS.pop(key, None)
    _EXITS.pop(key, None)
    state.error = None
    return None


def stop(project_root: str) -> str | None:
    """Suppress deferred restoration, then confirm a graceful exit or contained tree cleanup."""
    state = _project(key := _normalize(project_root))
    with state.lock:
        state.revision += 1
        state.disabled = True
        state.pending = None
        return _stop_process(key, state)


def stop_all() -> str | None:
    """Stop current watchers and suppress existing reservations, not future load/start calls."""
    roots = _roots()
    problems = []
    for root in roots:
        state = _project(root)
        with state.lock:
            for token in state.pauses:
                token._cancelled = True
            if problem := stop(root):
                problems.append(problem)
    if problems:
        problem = "\n".join(problems)
        print(f"[paradise_assets] {problem}")
        return problem
    return None


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


@dataclass(frozen=True)
class Rebuild:
    """What the watcher's latest rebuild said: whether it failed, its tally line, and every
    diagnostic it printed, as (``"error"`` | ``"warning"``, message) in log order."""

    failed: bool
    summary: str
    diagnostics: tuple[tuple[str, str], ...]


#: log path -> ((mtime_ns, size), answer), as for :data:`_LAST_ERRORS`.
_REBUILDS: dict[str, tuple[tuple[int, int], Rebuild | None]] = {}

#: A failed rebuild of a whole project can print hundreds of diagnostics; the panel lists them all.
_REBUILD_TAIL_BYTES = 1024 * 1024

_RESULT = re.compile(r"^watch: (build FAILED|rebuilt) ")
_DIAGNOSTIC = re.compile(r"^(error|warning): ?(.*)$")


def last_rebuild(project_root: str) -> Rebuild | None:
    """The watcher's latest finished rebuild, or ``None`` before the first one."""
    path = log_path(project_root)
    try:
        stat = os.stat(path)
    except OSError:
        return None
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _REBUILDS.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    answer = _scan_rebuild(path, _assets_prefix(project_root))
    _REBUILDS[path] = (stamp, answer)
    return answer


def _scan_rebuild(path: str, assets: str) -> Rebuild | None:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - _REBUILD_TAIL_BYTES))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    ends = [index for index, line in enumerate(lines) if _RESULT.match(line.strip())]
    if not ends:
        return None
    # A rebuild's diagnostics are printed before its tally, after the previous rebuild's.
    last = ends[-1]
    first = ends[-2] + 1 if len(ends) > 1 else 0
    diagnostics = []
    for line in lines[first:last]:
        found = _DIAGNOSTIC.match(line.strip())
        if found:
            diagnostics.append((found.group(1), _tidy(found.group(2), assets)))
    summary = lines[last].strip().removeprefix("watch: ")
    return Rebuild(summary.startswith("build FAILED"), summary, tuple(diagnostics))


def _assets_prefix(project_root: str) -> str:
    return os.path.join(os.path.realpath(project_root), "assets") + os.sep


def _tidy(message: str, assets: str) -> str:
    """A diagnostic as an author reads it: the CLI repeats its ``error:`` prefix and the file it
    names (``error: a.mesh: a.mesh: what``), and names files by their absolute path."""
    while message.startswith(("error: ", "warning: ")):
        message = message.split(": ", 1)[1]
    message = message.replace(assets, "")
    head, _, rest = message.partition(": ")
    while rest.startswith(head + ": "):
        rest = rest[len(head) + 2:]
    return f"{head}: {rest}" if rest else message


def exit_reason(project_root: str) -> str | None:
    """Why the watcher stopped, or ``None`` if it never started or is still running."""
    state = _project(_normalize(project_root))
    with state.lock:
        if state.unsafe or state.error:
            return state.unsafe or state.error
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
    from .materialize import refresh, store, workfile

    _adopting = True
    try:
        seen: set[str] = set()
        for scene in bpy.data.scenes:
            problem = workfile.refresh_from_document(scene)
            if problem:
                refresh.deferred(scene)
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
        for root in _roots():
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
    global _ENABLED
    with _REGISTRY_LOCK:
        _ENABLED = True
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
    global _ENABLED
    with _REGISTRY_LOCK:
        _ENABLED = False
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
    state = _project(key := _normalize(project_root))
    paused = (
        "The asset watcher is paused for Build & Play; retry after the build releases it. "
        "The watcher cannot mint an identity while paused, so nothing can be created without it."
    )
    if state.pauses:
        return paused
    with state.lock:
        if state.pauses:
            return paused
        if is_running(key) and not state.unsafe:
            return None
        problem = start(key)
        if problem is None:
            if is_running(key):
                return None
            problem = "The asset watcher exited before it could mint an identity."
    return (
        f"{problem} The asset watcher is what gives a new prefab its identity, so nothing can "
        "be created without it."
    )
