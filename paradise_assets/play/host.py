"""Finding and running the asset CLI and the game.

Duplicated from ``paradise_blender/play/host.py`` because extensions cannot import each other
(#35). The CLI runs to completion and captured, so a failed build stops the launch instead of
handing the runtime last build's tree; the runtime launches detached, so its output goes to a
log and :func:`first_error_line` has to make that readable.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile

__all__ = [
    "CliResult",
    "ensure_cli_built",
    "first_error_line",
    "launch_runtime",
    "log_path",
    "resolve_cli_command",
    "resolve_runtime_command",
    "run_cli",
    "subprocess_environment",
]


def log_path() -> str:
    return os.path.join(tempfile.gettempdir(), "paradise_assets_play.log")


def _well_known_dotnet() -> str | None:
    candidates = [
        "/usr/local/share/dotnet/dotnet",
        "/opt/homebrew/bin/dotnet",
        os.path.expanduser("~/.dotnet/dotnet"),
        r"C:\Program Files\dotnet\dotnet.exe",
    ]
    return next((c for c in candidates if os.path.exists(c)), None)


def _dotnet_run(project: str, *, no_build: bool = False) -> list[str] | None:
    """``dotnet run --project <csproj> --``, or None without the SDK. ``no_build`` for the CLI:
    the watcher is a live ``dotnet run`` of the same csproj, and a second one that builds dies
    on MSB0001 ``Invalid node id specified``."""
    if not os.path.exists(project):
        return None
    dotnet = shutil.which("dotnet") or _well_known_dotnet()
    if dotnet is None:
        return None
    argv = [dotnet, "run"]
    if no_build:
        argv.append("--no-build")
    argv.extend(["--project", project, "--"])
    return argv


def _configured(value: str, *, no_build: bool = False) -> list[str] | None:
    """A configured path as an argv prefix. ``realpath``, not ``abspath``: MSBuild resolves
    ProjectReferences against the canonical directory, so a symlinked csproj dies on MSB3202,
    and Blender's file browser walks into symlinks happily."""
    resolved = os.path.realpath(os.path.expanduser(value.strip()))
    if resolved.endswith(".csproj"):
        return _dotnet_run(resolved, no_build=no_build)
    return [resolved] if os.path.exists(resolved) else None


def _ladder(configured: str, command_name: str, *, no_build: bool = False) -> list[str] | None:
    """Configured path, then PATH, then the installed dotnet tool."""
    if configured.strip():
        found = _configured(configured, no_build=no_build)
        if found is not None:
            return found

    on_path = shutil.which(command_name)
    if on_path:
        return [on_path]

    tool = os.path.join(
        os.path.expanduser("~"),
        ".dotnet",
        "tools",
        f"{command_name}.exe" if os.name == "nt" else command_name,
    )
    return [tool] if os.path.exists(tool) else None


def _preference(name: str, default: str = "") -> str:
    from ..prefs import get_preferences

    preferences = get_preferences()
    if preferences is None:
        return default
    try:
        return getattr(preferences, name) or default
    except AttributeError:
        return default


def resolve_cli_command() -> list[str] | None:
    """Argv prefix for the ``paradise`` CLI, or ``None``."""
    return _ladder(_preference("cli"), "paradise", no_build=True)


def resolve_runtime_command() -> list[str] | None:
    """Argv prefix for the game, or ``None``. Configured or nothing: guessing a name would find
    some other project's launcher on PATH."""
    configured = _preference("runtime_host")
    return _configured(configured) if configured.strip() else None


def subprocess_environment() -> dict[str, str]:
    """Child environment with the MSBuild server off: two ``dotnet run`` sharing one die on
    MSB0001, which is what Play looked like beside a live watcher."""
    environment = dict(os.environ)
    environment["DOTNET_CLI_DO_NOT_USE_MSBUILD_SERVER"] = "1"
    ktx = _preference("ktx_path").strip()
    if ktx:
        environment["PARADISE_KTX_PATH"] = os.path.realpath(os.path.expanduser(ktx))
    return environment


def _cli_csproj() -> str | None:
    configured = _preference("cli").strip()
    if not configured:
        return None
    resolved = os.path.realpath(os.path.expanduser(configured))
    if resolved.endswith(".csproj") and os.path.exists(resolved):
        return resolved
    return None


def ensure_cli_built() -> str | None:
    """Compile the CLI csproj if ``--no-build`` would have nothing to run; a string is why not."""
    project = _cli_csproj()
    if project is None:
        return None
    output = os.path.join(os.path.dirname(project), "bin", "Debug", "net10.0", "paradise.dll")
    if os.path.isfile(output):
        return None

    dotnet = shutil.which("dotnet") or _well_known_dotnet()
    if dotnet is None:
        return "No dotnet SDK found to build the Paradise CLI."

    try:
        completed = subprocess.run(
            [dotnet, "build", project, "-v", "q", "--nologo"],
            capture_output=True,
            text=True,
            env=subprocess_environment(),
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as error:
        return f"Could not build the Paradise CLI: {error}"

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        last = next((line for line in reversed(detail) if line.strip()), "build failed")
        return last
    return None


class CliResult:
    """What a finished CLI run said."""

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def summary(self, limit: int = 300) -> str:
        """The first ``error:`` line, since the CLI's closing summary names no file."""
        lines = [line.strip() for line in (self.stderr + "\n" + self.stdout).splitlines() if line.strip()]
        if not lines:
            return f"exit code {self.returncode}"
        best = next((line for line in lines if line.lower().startswith("error:")), None)
        if best is None:
            best = next((line for line in reversed(lines) if ":" in line), lines[-1])
        return best if len(best) <= limit else best[: limit - 3] + "..."


def run_cli(arguments: list[str], cwd: str, timeout: float = 900.0) -> CliResult | None:
    """Run the CLI to completion in ``cwd``; ``None`` when it could not start. Synchronous so a
    failed build can stop the launch."""
    command = resolve_cli_command()
    if command is None:
        return None

    environment = subprocess_environment()
    problem = ensure_cli_built()
    if problem:
        return CliResult(-1, "", f"error: {problem}")

    try:
        completed = subprocess.run(
            [*command, *arguments],
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return CliResult(-1, "", f"error: could not run the Paradise CLI: {error}")

    return CliResult(completed.returncode, completed.stdout or "", completed.stderr or "")


def launch_runtime(arguments: list[str], cwd: str) -> tuple[subprocess.Popen | None, str | None]:
    """Launch the runtime detached; returns ``(process, error)``.

    The process, not a pid, so a caller can ``poll()`` an immediate death that would otherwise
    look like a clean launch. ``cwd`` is required: a detached child inherits Blender's working
    directory, which is ``/`` when launched from the Dock, and the game dies looking for its
    config. Only the POSIX branch writes the log; a Windows runtime crash reports only its exit
    code.
    """
    command = resolve_runtime_command()
    if command is None:
        return None, (
            "No runtime host is set. Point 'Runtime Host' in the addon preferences at the game's "
            "launcher -- an executable, or its .csproj to run via `dotnet run --project`."
        )

    argv = [*command, *arguments, *shlex.split(_preference("runtime_arguments"))]
    environment = subprocess_environment()

    try:
        if os.name == "nt":
            process = subprocess.Popen(  # argv is built from resolved paths
                argv,
                cwd=cwd,
                env=environment,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            # A GUI-launched Blender has no dotnet directory on PATH, which the child build
            # steps need.
            dotnet_dir = os.path.dirname(shutil.which("dotnet") or "/usr/local/share/dotnet/dotnet")
            quoted = " ".join(shlex.quote(a) for a in argv)
            script = f'export PATH="{dotnet_dir}:$PATH"; exec {quoted} > {shlex.quote(log_path())} 2>&1'
            process = subprocess.Popen(
                ["/bin/sh", "-c", script],
                cwd=cwd,
                env=environment,
                start_new_session=True,
            )
    except OSError as error:
        return None, f"Failed to launch the runtime: {error}"

    return process, None


# "error" catches MSBuild, the exception words catch a .NET crash banner.
_FAILURE_MARKERS = ("error", "exception", "unhandled", "fatal")


def first_error_line(path: str) -> str | None:
    """The most explanatory line of a failed run's log. Falls back to the FIRST non-warning
    line: a launcher prints its cause first and hints after, and the first line of a
    ``dotnet run`` log is usually an irrelevant SDK warning that would read as the diagnosis."""
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
