"""A fail-closed Build & Play session, independent of Blender's UI event loop.

Always build assets and then the launcher before asking ``host play`` to run.
The CLI's extension-based launcher freshness shortcut cannot account for arbitrary inputs.
Watch failures end the whole owned process tree, including a game running the previous build.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import os
import re
import signal
import subprocess
import tempfile
import threading

from .. import watch as asset_watch
from . import process_tree

__all__ = [
    "exit_reason",
    "first_error_line",
    "is_running",
    "log_path",
    "needs_recovery",
    "play_command",
    "process_for",
    "start",
    "stop",
    "stop_all",
]

_SESSIONS: dict[str, PlaySession] = {}

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


def play_command(
    cli_argv: list[str], project_root: str, document_path: str, *, watch: bool, profile: str | None = None,
) -> list[str]:
    """Name the document, not its built twin. Assets must have passed the preceding build stage."""
    from .host import _preference

    if profile is None:
        profile = _preference("build_profile", "dev") or "dev"
    argv = [
        *cli_argv, "host", "play", "--no-assets",
        "--profile", profile,
        "--scene", document_path,
        "--project", project_root,
    ]
    if watch:
        argv.append("--watch")
    return argv


def process_for(project_root: str) -> PlaySession | None:
    """The live session's process, or ``None``; reaps one that has exited."""
    key = _normalize(project_root)
    process = _SESSIONS.get(key)
    if process is None:
        return None
    if (code := process.poll()) is not None:
        # A failed cleanup still owns the watcher pause. Keep it for Stop/replacement
        # recovery, without presenting this completed worker as a running game.
        if process._asset_pause is None:
            _SESSIONS.pop(key, None)
        failed = code not in (0, INTERRUPTED)
        _EXITS[key] = (code, process.detail if failed else None)
        return None
    return process


def is_running(project_root: str) -> bool:
    return process_for(project_root) is not None


def needs_recovery(project_root: str) -> bool:
    """A completed worker still owns cleanup and its asset-watcher reservation."""
    process = _SESSIONS.get(_normalize(project_root))
    return process is not None and process.poll() is not None and process._asset_pause is not None


class PlaySession:
    """One worker owns every stage and its cleanup; polling never launches work from a draw."""

    def __init__(
        self, root: str, stages: list[tuple[str, list[str]]], environment: dict,
        *, watch: bool, asset_pause: asset_watch.WatchPause | None = None,
    ):
        self.root = root
        self.status = stages[0][0]
        self.detail: str | None = None
        self.returncode: int | None = None
        self._stages = stages
        self._environment = environment
        self._watch = watch
        self._asset_pause = asset_pause
        self._cleanup_failed = False
        self._process: subprocess.Popen | None = None
        self._ownership: process_tree.Ownership | None = None
        self._stopping = threading.Event()
        self._done = threading.Event()
        self._diagnostic: str | None = None
        self._first_line: str | None = None
        self._watch_error: str | None = None
        self._pending = b""
        self._playing = False
        # Open before returning from start(), so an unwritable log is an immediate error.
        self._output = open(log_path(root), "wb")  # noqa: SIM115 -- worker owns the handle
        self._thread = threading.Thread(target=self._run, name="paradise-play", daemon=True)
        try:
            self._thread.start()
        except RuntimeError:
            self._output.close()
            raise

    @property
    def pid(self) -> int:
        return self._process.pid if self._process is not None else 0

    @property
    def reason(self) -> str | None:
        if self.poll() in (None, 0, INTERRUPTED):
            return None
        return f"exit {self.returncode}: {self.detail}" if self.detail else f"exit {self.returncode}"

    def poll(self) -> int | None:
        return self.returncode if self._done.is_set() else None

    def wait(self, timeout: float | None = None) -> int:
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("Build & Play", timeout)
        return self.returncode

    def cancel(self) -> None:
        self._stopping.set()

    def _run(self) -> None:
        try:
            with self._output, open(log_path(self.root), "rb") as reader:
                if self._asset_pause is not None and (problem := self._asset_pause.pause()):
                    raise RuntimeError(problem)
                for index, (label, argv) in enumerate(self._stages):
                    if self._stopping.is_set():
                        self.returncode = INTERRUPTED
                        break
                    self.status = label
                    self.returncode = None
                    self._playing = index == len(self._stages) - 1
                    self._diagnostic = None
                    self._first_line = None
                    self._watch_error = None
                    self._ownership = None
                    if self._playing and (problem := self._resume_assets()):
                        raise RuntimeError(problem)
                    options = process_tree.launch_options(self._environment)
                    self._process = subprocess.Popen(
                        argv, cwd=self.root, stdout=self._output,
                        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **options,
                    )
                    try:
                        self._ownership = process_tree.record(
                            self.root, self._process, environment=options["env"],
                        )
                    except OSError:
                        # Still our unreaped child: its PID/group cannot yet have been reused.
                        _kill_unrecorded(self._process)
                        raise
                    self.returncode = self._monitor(reader)
                    self._cleanup_failed = True
                    problem = self._clean_tree()
                    self._cleanup_failed = bool(problem)
                    if problem:
                        self.returncode, self.detail = 1, problem
                    elif self.returncode not in (0, INTERRUPTED):
                        self.detail = self._watch_error or self._diagnostic or self._first_line
                    if self.returncode != 0:
                        break
        except BaseException as error:
            # A worker owns its children even on SystemExit. Publish failure before cleanup,
            # which can itself fail; a completed worker must never look permanently alive.
            self.returncode = 1
            self.detail = f"Could not run Build & Play: {error}"
            self._cleanup_failed = self._process is not None
            try:
                problem = self._clean_tree() if self._process is not None else None
                self._cleanup_failed = bool(problem)
            except BaseException as cleanup_error:
                problem = f"Could not clean up the play process: {cleanup_error}"
            if problem:
                self.detail += f"; {problem}"
        finally:
            if self.returncode is None:
                self.returncode = 1
                self.detail = self.detail or "Build & Play worker ended unexpectedly."
            try:
                if problem := self._resume_assets():
                    self.returncode = 1
                    self.detail = f"{self.detail}; {problem}" if self.detail else problem
            except BaseException as resume_error:
                self.returncode = 1
                problem = f"Could not restore the asset watcher: {resume_error}"
                self.detail = f"{self.detail}; {problem}" if self.detail else problem
            finally:
                self._done.set()

    def _resume_assets(self) -> str | None:
        if self._asset_pause is not None and self._cleanup_failed:
            return (
                "Asset watcher remains paused because build cleanup is unconfirmed; "
                "resolve the cleanup error and retry Stop or Play."
            )
        pause, self._asset_pause = self._asset_pause, None
        return pause.resume() if pause is not None else None

    def _clean_tree(self) -> str | None:
        problem = process_tree.release(self.root, self._ownership) if self._ownership is not None else None
        if self._process is not None:
            try:
                self._process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                return problem or "The earlier game did not stop; close it before playing again."
        return problem

    def _monitor(self, reader) -> int:
        while True:
            self._read_output(reader)
            if self._watch_error:
                return 1
            if self._stopping.is_set():
                return INTERRUPTED
            code = self._process.poll()
            if code is not None:
                self._read_output(reader, final=True)
                return 1 if self._watch_error else code
            self._stopping.wait(0.1)

    def _read_output(self, reader, *, final: bool = False) -> None:
        # Drain available output without sleeping between chunks: a chatty game must not hide a
        # later watch failure behind a growing backlog. Only complete lines are interpreted.
        while chunk := reader.read(_HEAD_BYTES):
            lines = (self._pending + chunk).split(b"\n")
            self._pending = lines.pop()
            for raw in lines:
                self._observe(raw.decode("utf-8", errors="replace"))
            if self._watch_error or self._stopping.is_set():
                return
        if final and self._pending:
            self._observe(self._pending.decode("utf-8", errors="replace"))
            self._pending = b""

    def _observe(self, line: str) -> None:
        line = _ANSI.sub("", line).strip()
        if self._first_line is None and line and not _is_build_noise(line):
            self._first_line = line[:300]
        if self._diagnostic is None and _is_diagnostic(line):
            self._diagnostic = line[:300]
        if not self._playing:
            return
        if self._watch and self._watch_error is None and _watch_failed(line):
            self._watch_error = line[:300]


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# The game shares this stream; runtime/shader codes are not .NET build failures.
_COMPILER_ERROR = re.compile(
    r"(?:^|:\s*)(?i:error)\s+(?:CS|BC|FS|MSB|MSBUILD|NU|NETSDK)\d+\s*:"
)


def _watch_failed(line: str) -> bool:
    lowered = line.lower()
    return (
        bool(_COMPILER_ERROR.search(line))
        or lowered == "build failed."
        or ("dotnet watch" in lowered and (
            "build failed" in lowered or "failed to build" in lowered
            or ("error(s)" in lowered and "0 error(s)" not in lowered)
        ))
    )


def _kill_unrecorded(process: subprocess.Popen) -> None:
    with contextlib.suppress(OSError):
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=2.0)


def start(
    project_root: str, document_path: str, *, watch: bool
) -> tuple[PlaySession | None, str | None]:
    """Replace the whole earlier tree before building, including a prior Blender's orphan."""
    from . import host

    command = host.resolve_cli_command(project_root)
    if command is None:
        return None, (
            "No `paradise` CLI found. Install it as a dotnet tool, or point 'Paradise CLI' in "
            "the addon preferences at it."
        )
    if problem := stop(project_root):
        return None, problem

    stages = []
    if cli_build := host._build_stage(project_root):
        stages.append(("Building Paradise CLI", cli_build))
    profile = host._preference("build_profile", "dev") or "dev"
    stages.append(("Building assets", [
        *command, "assets", "build", "--editor", "--profile", profile, "--project", project_root,
    ]))
    # Asset generation can change launcher inputs, so build those first. Unlike host play's
    # freshness scan, host build lets MSBuild track Content/EmbeddedResource/native files too.
    stages.append(("Building launcher", [*command, "host", "build", "--project", project_root]))
    stages.append(("Watch & Play session" if watch else "Play session", play_command(
        command, project_root, document_path, watch=watch, profile=profile,
    )))
    environment = host.subprocess_environment()
    # Watch's text protocol has no structured status API in the supported CLI versions.
    environment.update({"DOTNET_CLI_UI_LANGUAGE": "en-US", "DOTNET_WATCH_SUPPRESS_EMOJIS": "1"})
    asset_pause = None
    try:
        asset_pause = asset_watch.prepare_pause(project_root, command, environment)
        process = PlaySession(project_root, stages, environment, watch=watch, asset_pause=asset_pause)
    except (OSError, RuntimeError) as error:
        problem = asset_pause.resume() if asset_pause is not None else None
        detail = f"Could not start Build & Play: {error}"
        return None, f"{detail}; {problem}" if problem else detail
    key = _normalize(project_root)
    _SESSIONS[key] = process
    _EXITS.pop(key, None)
    return process, None


def stop(project_root: str) -> str | None:
    """Cancel, join and reclaim ownership before a replacement may truncate the old log."""
    key = _normalize(project_root)
    process = _SESSIONS.get(key)
    if process is not None:
        process.cancel()
        try:
            process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            return "The earlier play session is still stopping; try again after it exits."
    if problem := process_tree.stop(project_root):
        return problem
    if process is not None and process._asset_pause is not None:
        if process._process is not None:
            try:
                process._process.wait(timeout=2.0)
            except (OSError, subprocess.SubprocessError) as error:
                return f"Cannot confirm the earlier build stopped: {error}"
        process._cleanup_failed = False
        if problem := process._resume_assets():
            return problem
    _SESSIONS.pop(key, None)
    _EXITS.pop(key, None)
    return None


def stop_all() -> None:
    """Stop every session. Registered with ``atexit`` and called on unregister."""
    for root in list(_SESSIONS):
        stop(root)


def exit_reason(project_root: str) -> str | None:
    """Why the last session ended badly, or ``None`` after a clean exit, a Stop, or never."""
    process_for(project_root)
    entry = _EXITS.get(_normalize(project_root))
    if entry is None:
        return None
    code, detail = entry
    if code in (0, INTERRUPTED):
        return None
    return f"exit {code}: {detail}" if detail else f"exit {code}"


#: Incremental reader chunk size; the standalone log helper also uses this as its head limit.
_HEAD_BYTES = 64 * 1024

#: A .NET crash banner, as a whole-word marker; "error" alone would match MSBuild's tally.
_CRASH_MARKERS = ("unhandled exception", "fatal error", "exception:")


def first_error_line(path: str) -> str | None:
    """The most explanatory line of a failed run's log: the first diagnostic (``error:`` from
    the CLI, ``error CS1002:``/``error MSB4025:`` from MSBuild, a crash banner), else the FIRST
    non-warning line -- a launcher prints its cause first and hints after, and the first line of
    a build log is usually an irrelevant SDK warning that would read as the diagnosis.

    Kept as the exported standalone log helper; live sessions track diagnostics per stage.
    """
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


#: Ordinary shutdown joins the worker; durable process ownership also covers a crashed Blender.
atexit.register(stop_all)
