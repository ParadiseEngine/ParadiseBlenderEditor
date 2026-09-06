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
import hashlib
import os
import subprocess
import tempfile

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
        return None, f"Could not start the game: {error}"
    finally:
        # On Windows an open handle here would lock the log against the next run's truncate.
        handle.close()

    _SESSIONS[_normalize(project_root)] = process
    _EXITS.pop(_normalize(project_root), None)
    return process, None


def stop(project_root: str) -> None:
    """Terminate, wait briefly, then kill; Blender must not block on it. On POSIX the CLI
    handles SIGTERM by killing the game's whole process tree; on Windows ``terminate()`` IS
    ``TerminateProcess`` and no handler runs, so the game may outlive the CLI there."""
    key = _normalize(project_root)
    process = _SESSIONS.pop(key, None)
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


# "error" catches MSBuild, the exception words catch a .NET crash banner.
_FAILURE_MARKERS = ("error", "exception", "unhandled", "fatal")


def first_error_line(path: str) -> str | None:
    """The most explanatory line of a failed run's log. Falls back to the FIRST non-warning
    line: a launcher prints its cause first and hints after, and the first line of a build log
    is usually an irrelevant SDK warning that would read as the diagnosis."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = [line.strip() for line in handle if line.strip()]
    except OSError:
        return None

    if not lines:
        return None

    match = next((line for line in lines if any(m in line.lower() for m in _FAILURE_MARKERS)), None)
    if match is None:
        match = next((line for line in lines if not _is_build_noise(line)), lines[0])
    return match if len(match) <= 300 else match[:297] + "..."


def _is_build_noise(line: str) -> bool:
    """A compiler/SDK warning (``<origin>: warning <CODE>:``). Shape-matched, not a bare word
    search, or a launcher's own fatal line containing "warning" would be swallowed."""
    return ": warning " in line.lower()


#: Quitting is the case unregister cannot see; a crash or SIGKILL is covered by nothing.
atexit.register(stop_all)
