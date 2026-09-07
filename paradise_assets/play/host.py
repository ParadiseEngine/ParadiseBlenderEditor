"""Finding and running the ``paradise`` CLI.

The CLI is the only program this extension runs: building, verifying, watching
AND playing are all its verbs, so the game's launcher is the CLI's business (``[host]`` in
``assets/project.toml``), never a preference here. A verb runs to completion and captured, except
the long-lived ones (:mod:`..watch`, :mod:`.session`), which are supervised children.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess

__all__ = [
    "CliJob",
    "CliResult",
    "ensure_cli_built",
    "project_engine_version",
    "resolve_cli_command",
    "run_cli",
    "start_cli",
    "subprocess_environment",
]


def _well_known_dotnet() -> str | None:
    candidates = [
        "/usr/local/share/dotnet/dotnet",
        "/opt/homebrew/bin/dotnet",
        os.path.expanduser("~/.dotnet/dotnet"),
        os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "dotnet", "dotnet.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), "dotnet", "dotnet.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "dotnet", "dotnet.exe"),
        os.path.expanduser("~/.dotnet/dotnet.exe"),
    ]
    return next((c for c in candidates if os.path.exists(c)), None)


def _dotnet() -> str | None:
    return shutil.which("dotnet") or _well_known_dotnet()


def _built_output(project: str) -> str:
    return os.path.join(os.path.dirname(project), "bin", "Debug", "net10.0", "paradise.dll")


def _dotnet_run(project: str) -> list[str] | None:
    """The built ``paradise.dll`` straight under ``dotnet`` when it exists (half a second faster
    than ``dotnet run``, which evaluates the project first), else ``dotnet run --no-build``:
    the watcher is a live run of the same csproj, and a second one that builds dies on MSB0001
    ``Invalid node id specified``. ``None`` without the SDK."""
    if not os.path.exists(project):
        return None
    dotnet = _dotnet()
    if dotnet is None:
        return None
    output = _built_output(project)
    if os.path.isfile(output):
        return [dotnet, output]
    return [dotnet, "run", "--no-build", "--project", project, "--"]


def _configured(value: str) -> list[str] | None:
    """A configured path as an argv prefix. ``realpath``, not ``abspath``: MSBuild resolves
    ProjectReferences against the canonical directory, so a symlinked csproj dies on MSB3202,
    and Blender's file browser walks into symlinks happily."""
    resolved = os.path.realpath(os.path.expanduser(value.strip()))
    if resolved.endswith(".csproj"):
        return _dotnet_run(resolved)
    return [resolved] if os.path.exists(resolved) else None


#: One directory per version, shared by every project on the machine. NOT the global tool
#: (``~/.dotnet/tools``): that is a single slot, and installing into it for one project changes
#: which CLI every other project gets.
_TOOL_CACHE = os.path.join(os.path.expanduser("~"), ".paradise", "cli")

_PARADISE_VERSION = re.compile(r"<ParadiseVersion>\s*([^<\s]+)\s*</ParadiseVersion>")


def project_engine_version(project_root: str) -> str | None:
    """
    The engine version a project pins, read from ``Directory.Packages.props``.

    The same one number the release pipeline reads, and for the same reason: the CLI that writes
    a project's documents and the runtime that reads them should be one engine build by
    construction. ``None`` for a tree that pins nothing that way, which falls back to whatever
    CLI is installed.
    """
    try:
        text = pathlib.Path(project_root, "Directory.Packages.props").read_text(encoding="utf-8")
    except OSError:
        return None
    found = _PARADISE_VERSION.search(text)
    return found.group(1) if found else None


def _versioned_cli(version: str) -> list[str] | None:
    """``paradise`` at exactly *version*, fetched once into its own directory. ``None`` when it is
    not there and cannot be fetched -- offline, or a version that was never published."""
    directory = os.path.join(_TOOL_CACHE, version)
    binary = os.path.join(directory, "paradise.exe" if os.name == "nt" else "paradise")
    if os.path.exists(binary):
        return [binary]

    dotnet = _dotnet()
    if dotnet is None:
        return None
    try:
        completed = subprocess.run(
            [dotnet, "tool", "install", "Paradise.Cli", "--version", version, "--tool-path", directory],
            capture_output=True,
            text=True,
            timeout=600,
            env=_with_dotnet_on_path(),
        )
    except (OSError, subprocess.SubprocessError) as error:
        print(f"[paradise_assets] could not fetch paradise {version}: {error}")
        return None
    if completed.returncode != 0:
        print(f"[paradise_assets] could not fetch paradise {version}: {completed.stderr.strip()}")
        return None
    return [binary] if os.path.exists(binary) else None


def _ladder(configured: str, command_name: str) -> list[str] | None:
    """Configured path, then PATH, then the installed dotnet tool."""
    if configured.strip():
        found = _configured(configured)
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


def resolve_cli_command(project_root: str | None = None) -> list[str] | None:
    """
    Argv prefix for the ``paradise`` CLI, or ``None``.

    With a *project_root*, the CLI is the one that project PINS -- fetched on first use and kept
    per version -- because a CLI older than the tree writes documents the runtime cannot read, and
    one that cannot read the manifest falls back to defaults and reports a cascade of errors about
    everything except the version. The configured preference still wins, so pointing the addon at
    a source build stays possible; a pinned version that cannot be fetched warns and falls through
    rather than stopping work offline.
    """
    configured = _preference("cli")
    if configured.strip():
        found = _configured(configured)
        if found is not None:
            return found

    if project_root:
        version = project_engine_version(project_root)
        if version is not None:
            pinned = _versioned_cli(version)
            if pinned is not None:
                return pinned
            print(f"[paradise_assets] paradise {version} unavailable; using whatever is installed")

    return _ladder("", "paradise")


def _with_dotnet_on_path() -> dict[str, str]:
    """``os.environ`` plus the dotnet directory on PATH: a Dock-launched Blender has none, and
    MSBuild's own ``dotnet exec`` steps need it. Separate from :func:`subprocess_environment`
    because fetching a tool needs this and nothing else -- in particular not a preference, which
    would drag ``bpy`` into a path that has to work while Blender is starting."""
    environment = dict(os.environ)
    dotnet = _dotnet()
    if dotnet is not None:
        directory = os.path.dirname(os.path.realpath(dotnet))
        current = environment.get("PATH", "")
        if directory not in current.split(os.pathsep):
            environment["PATH"] = directory + os.pathsep + current if current else directory
    return environment


def subprocess_environment() -> dict[str, str]:
    """Child environment for a CLI verb: dotnet on PATH and the MSBuild server off, since two
    ``dotnet`` builds sharing one die on MSB0001 -- which is what Play looked like beside a live
    watcher."""
    environment = _with_dotnet_on_path()
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


def _build_stage() -> list[str] | None:
    """The ``dotnet build`` the CLI needs before it has anything to run, or ``None``."""
    project = _cli_csproj()
    if project is None or os.path.isfile(_built_output(project)):
        return None
    dotnet = _dotnet()
    if dotnet is None:
        return None
    return [dotnet, "build", project, "-v", "q", "--nologo"]


def ensure_cli_built() -> str | None:
    """Compile the CLI csproj if there is nothing to run yet; a string is why not."""
    stage = _build_stage()
    if stage is None:
        if _cli_csproj() is not None and _dotnet() is None:
            return "No dotnet SDK found to build the Paradise CLI."
        return None

    try:
        completed = subprocess.run(
            stage,
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


class CliJob:
    """A CLI run in progress, polled from a modal timer so a minutes-long build never freezes
    the UI. When the CLI csproj has no build output yet, ``dotnet build`` runs first as a stage
    of the same job."""

    def __init__(self, stages: list[list[str]], cwd: str) -> None:
        self._stages = stages
        self._cwd = cwd
        self._process: subprocess.Popen | None = None
        self.result: CliResult | None = None
        self._advance()

    def _advance(self) -> None:
        argv = self._stages.pop(0)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self._process = subprocess.Popen(  # argv is built from resolved paths
                argv,
                cwd=self._cwd,
                env=subprocess_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                creationflags=flags,
            )
        except (OSError, subprocess.SubprocessError) as error:
            self._process = None
            self.result = CliResult(-1, "", f"error: could not run the Paradise CLI: {error}")

    def poll(self) -> CliResult | None:
        """The result once every stage has finished, else ``None``."""
        if self.result is not None:
            return self.result
        if self._process is None or self._process.poll() is None:
            return None
        # Read after exit: a pipe read here cannot block, and the CLI's output is small.
        output = self._process.stdout.read() if self._process.stdout else ""
        self._process.stdout and self._process.stdout.close()
        code = self._process.returncode
        if code != 0 or not self._stages:
            self.result = CliResult(code, output, "")
            return self.result
        self._advance()
        return self.result

    def cancel(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()


def start_cli(arguments: list[str], cwd: str) -> CliJob | None:
    """Start the CLI in ``cwd`` without waiting; ``None`` when there is no CLI to run."""
    command = resolve_cli_command(cwd)
    if command is None:
        return None
    stages = []
    build = _build_stage()
    if build is not None:
        stages.append(build)
    stages.append([*command, *arguments])
    return CliJob(stages, cwd)


def run_cli(arguments: list[str], cwd: str, timeout: float = 900.0) -> CliResult | None:
    """Run the CLI to completion in ``cwd``; ``None`` when it could not start. Synchronous, for
    background Blender (no event loop for a modal) and scripts; the operators use
    :func:`start_cli`."""
    command = resolve_cli_command(cwd)
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
