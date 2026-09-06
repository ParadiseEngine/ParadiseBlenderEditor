"""The running game, supervised by Blender: ``paradise host play`` as a child.

One session per project root. The CLI is the process Blender holds -- it builds the assets, brings
the launcher up to date, runs the game and waits for it, and a SIGTERM to it takes the game down
with it (``dotnet watch`` included) -- so "Stop" is one ``terminate()`` and a second Play replaces
the first instead of stacking a window on it. Output goes to a log the panel reads the first
error from: a launcher prints its cause first and hints after, unlike the asset watcher's log,
whose latest rebuild is the interesting one.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import os
import signal
import subprocess
import tempfile
import threading

__all__ = [
    "exit_reason",
    "first_error_line",
    "is_running",
    "log_path",
    "play_command",
    "process_for",
    "start",
    "stop",
    "stop_all",
]

_SESSIONS: dict[str, subprocess.Popen] = {}

#: (exit code, first error line) per root, kept until the next start.
_EXITS: dict[str, tuple[int, str | None]] = {}

#: What ``paradise host play`` exits with when it was terminated: not a failure to report.
INTERRUPTED = 130


def _normalize(project_root: str) -> str:
    return os.path.normcase(os.path.abspath(project_root))


def log_path(project_root: str) -> str:
    """Per-root log path, hashed like the watcher's so two checkouts never share one."""
    name = os.path.basename(os.path.normpath(project_root)) or "project"
    digest = hashlib.sha1(_normalize(project_root).encode("utf-8")).hexdigest()[:6]
    return os.path.join(tempfile.gettempdir(), f"paradise_assets_play_{name}_{digest}.log")


def play_command(cli_argv: list[str], project_root: str, document_path: str, *, watch: bool) -> list[str]:
    """The verb: the DOCUMENT's path, not its built twin -- the CLI knows the play tree's
    layout and this extension need not. ``--watch`` hands the launcher to ``dotnet watch run``."""
    from .host import _preference

    profile = _preference("build_profile", "dev") or "dev"
    argv = [
        *cli_argv, "host", "play",
        "--profile", profile,
        "--scene", document_path,
        "--project", project_root,
    ]
    if watch:
        argv.append("--watch")
    return argv


def process_for(project_root: str) -> subprocess.Popen | None:
    """The live session's process, or ``None``; reaps one that has exited."""
    key = _normalize(project_root)
    process = _SESSIONS.get(key)
    if process is None:
        return None
    if (code := process.poll()) is not None:
        _SESSIONS.pop(key, None)
        failed = code not in (0, INTERRUPTED)
        _EXITS[key] = (code, first_error_line(log_path(project_root)) if failed else None)
        return None
    return process


def is_running(project_root: str) -> bool:
    return process_for(project_root) is not None


def start(
    project_root: str, document_path: str, *, watch: bool
) -> tuple[subprocess.Popen | None, str | None]:
    """Replace any running session with a new one on ``document_path``; ``(process, error)``.

    ``cwd`` is the project root: the CLI locates the project from it, and the game it runs
    inherits it -- a detached child of a Dock-launched Blender would otherwise start in ``/``.
    """
    from . import host

    stop(project_root)

    command = host.resolve_cli_command()
    if command is None:
        return None, (
            "No `paradise` CLI found. Install it as a dotnet tool, or point 'Paradise CLI' in "
            "the addon preferences at it."
        )

    problem = host.ensure_cli_built()
    if problem:
        return None, problem

    path = log_path(project_root)
    try:
        handle = open(path, "w", encoding="utf-8")  # noqa: SIM115 -- handed to the child, then closed
    except OSError as error:
        return None, f"Could not open the play log: {error}"

    argv = play_command(command, project_root, document_path, watch=watch)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        # Its own session on POSIX, so `stop` can signal the whole group -- the CLI, a dotnet
        # watch and the game -- rather than trust the CLI alone to pass a SIGTERM down.
        process = subprocess.Popen(  # argv is built from resolved paths
            argv,
            cwd=project_root,
            env=host.subprocess_environment(),
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
    except (OSError, subprocess.SubprocessError) as error:
        handle.close()
        return None, f"Could not start the game: {error}"
    finally:
        # On Windows an open handle here would lock the log against the next run's truncate.
        handle.close()

    _SESSIONS[_normalize(project_root)] = process
    _EXITS.pop(_normalize(project_root), None)
    return process, None


#: How long `stop` waits on the calling thread before handing the rest to a reaper.
_STOP_GRACE_SECONDS = 0.5
#: How long the reaper gives the tree before SIGKILL.
_STOP_KILL_AFTER_SECONDS = 5.0


def stop(project_root: str) -> None:
    """End the whole tree -- the CLI, a dotnet watch, the game -- and return quickly.

    POSIX: SIGTERM to the process group (the session is its own), SIGKILL from a reaper thread
    if it is still there after a grace. Windows: ``taskkill /T /F`` on the CLI's pid, since
    ``terminate()`` is ``TerminateProcess`` and would orphan the grandchildren. Blender's main
    thread waits half a second at most.
    """
    key = _normalize(project_root)
    process = _SESSIONS.pop(key, None)
    if process is None or process.poll() is not None:
        return

    if os.name == "nt":
        _taskkill(process.pid)
        return

    _signal_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=_STOP_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    threading.Thread(target=_reap, args=(process,), name="paradise-play-reaper", daemon=True).start()


def _reap(process: subprocess.Popen) -> None:
    try:
        process.wait(timeout=_STOP_KILL_AFTER_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_group(process, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2.0)


def _signal_group(process: subprocess.Popen, signum: int) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signum)
    except OSError:
        with contextlib.suppress(OSError):
            process.send_signal(signum)


def _taskkill(pid: int) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True, timeout=10.0, creationflags=flags, check=False,
        )


def stop_all() -> None:
    """Stop every session. Registered with ``atexit`` and called on unregister."""
    for root in list(_SESSIONS):
        stop(root)


def exit_reason(project_root: str) -> str | None:
    """Why the last session ended badly, or ``None`` after a clean exit, a Stop, or never."""
    entry = _EXITS.get(_normalize(project_root))
    if entry is None:
        return None
    code, detail = entry
    if code in (0, INTERRUPTED):
        return None
    return f"exit {code}: {detail}" if detail else f"exit {code}"


#: The log is the whole `host play` stream, and the cause is near its top: the asset build's
#: `error:`, MSBuild's `error CSxxxx`, or the launcher's first line. 64 KiB covers all three.
_HEAD_BYTES = 64 * 1024

#: A .NET crash banner, as a whole-word marker; "error" alone would match MSBuild's tally.
_CRASH_MARKERS = ("unhandled exception", "fatal error", "exception:")


def first_error_line(path: str) -> str | None:
    """The most explanatory line of a failed run's log: the first diagnostic (``error:`` from
    the CLI, ``error CS1002:``/``error MSB4025:`` from MSBuild, a crash banner), else the FIRST
    non-warning line -- a launcher prints its cause first and hints after, and the first line of
    a build log is usually an irrelevant SDK warning that would read as the diagnosis."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            head = handle.read(_HEAD_BYTES)
    except OSError:
        return None

    lines = [line.strip() for line in head.splitlines() if line.strip()]
    if not lines:
        return None

    match = next((line for line in lines if _is_diagnostic(line)), None)
    if match is None:
        match = next((line for line in lines if not _is_build_noise(line)), lines[0])
    return match if len(match) <= 300 else match[:297] + "..."


def _is_diagnostic(line: str) -> bool:
    """A line that NAMES a failure. MSBuild's ``0 Error(s)`` / ``1 Error(s)`` tallies and
    ``Build FAILED.`` are counts, not causes, and would otherwise win by coming first."""
    lowered = line.lower()
    if lowered.endswith("error(s)") or lowered == "build failed.":
        return False
    return (
        lowered.startswith("error")
        or ": error " in lowered
        or " error cs" in lowered
        or " error msb" in lowered
        or any(marker in lowered for marker in _CRASH_MARKERS)
    )


def _is_build_noise(line: str) -> bool:
    """A compiler/SDK warning (``<origin>: warning <CODE>:``). Shape-matched, not a bare word
    search, or a launcher's own fatal line containing "warning" would be swallowed."""
    return ": warning " in line.lower()


#: Quitting is the case unregister cannot see; a crash or SIGKILL is covered by nothing.
atexit.register(stop_all)
