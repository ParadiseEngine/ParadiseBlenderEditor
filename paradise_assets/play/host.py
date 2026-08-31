"""Finding and running the two external programs: the asset CLI, and the game.

**Ported from ``paradise_blender/play/host.py`` rather than imported from it, and the duplication
is deliberate.** The two addons are independent extensions and either may be installed without the
other, so a cross-import would work on this machine and break on the first one that installed only
this addon. What was worth copying is the part that was learned the hard way: the resolution
ladder, the ``.csproj`` branch, ``realpath`` before use, the POSIX shell wrapper, and the rule for
picking the one line of a failed log that explains anything.

The two programs are run in opposite ways, and the asymmetry is the point:

* **The CLI is run to completion, captured.** A build has an exit code and a reason, and Blender
  can show both. It also means a failed build stops the launch instead of quietly handing the
  runtime last build's tree.
* **The runtime is launched DETACHED.** It is an interactive window with its own lifetime; Blender
  must neither block on it nor die with it. The cost is that its output goes to a log rather than
  to us, which is what :func:`first_error_line` exists to make readable.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile

__all__ = [
    "CliResult",
    "first_error_line",
    "launch_runtime",
    "log_path",
    "resolve_cli_command",
    "resolve_runtime_command",
    "run_cli",
]


def log_path() -> str:
    return os.path.join(tempfile.gettempdir(), "paradise_assets_play.log")


# --------------------------------------------------------------------------------------
# Finding things
# --------------------------------------------------------------------------------------


def _well_known_dotnet() -> str | None:
    candidates = [
        "/usr/local/share/dotnet/dotnet",
        "/opt/homebrew/bin/dotnet",
        os.path.expanduser("~/.dotnet/dotnet"),
        r"C:\Program Files\dotnet\dotnet.exe",
    ]
    return next((c for c in candidates if os.path.exists(c)), None)


def _dotnet_run(project: str) -> list[str] | None:
    """``dotnet run --project <csproj> --``, or None when the SDK is not installed."""
    if not os.path.exists(project):
        return None
    dotnet = shutil.which("dotnet") or _well_known_dotnet()
    return None if dotnet is None else [dotnet, "run", "--project", project, "--"]


def _configured(value: str) -> list[str] | None:
    """Turn a configured path into an argv prefix.

    ``realpath``, not ``abspath``: a project reached through a symlink builds against the wrong
    tree. MSBuild relativizes ProjectReferences against the path as spelled and then resolves them
    against the canonicalized directory, so a symlinked project path silently retargets every
    relative reference and the build dies on MSB3202 -- and Blender's file browser walks into
    symlinks happily, so authors hit this by simply picking the file.
    """
    resolved = os.path.realpath(os.path.expanduser(value.strip()))
    if resolved.endswith(".csproj"):
        return _dotnet_run(resolved)
    return [resolved] if os.path.exists(resolved) else None


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


def resolve_cli_command() -> list[str] | None:
    """Argv prefix that runs the ``paradise`` CLI, or ``None`` when nothing is installed.

    ``paradise`` is packed as a dotnet tool (``PackAsTool``/``ToolCommandName``), so the PATH and
    ``~/.dotnet/tools`` rungs are what will find it once it ships. Until then the preference is
    pointed at a build of ``Paradise.Cli``.
    """
    return _ladder(_preference("cli"), "paradise")


def resolve_runtime_command() -> list[str] | None:
    """Argv prefix that runs the GAME, or ``None``.

    There is no autodetect rung worth having here: which program plays a project is the game's
    business, and guessing at a name would find some other project's launcher on PATH. Configured
    or nothing.
    """
    configured = _preference("runtime_host")
    return _configured(configured) if configured.strip() else None


# --------------------------------------------------------------------------------------
# Running the CLI
# --------------------------------------------------------------------------------------


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
        """The most useful line to put in front of the author.

        The CLI ends every verb with a summary line (``build: …``, ``verify: …``) and prints the
        detail above it, so on failure the DETAIL is what is wanted -- the first ``error:`` line --
        and the summary is only a fallback for a failure that named none.
        """
        lines = [line.strip() for line in (self.stderr + "\n" + self.stdout).splitlines() if line.strip()]
        if not lines:
            return f"exit code {self.returncode}"
        best = next((line for line in lines if line.lower().startswith("error:")), None)
        if best is None:
            best = next((line for line in reversed(lines) if ":" in line), lines[-1])
        return best if len(best) <= limit else best[: limit - 3] + "..."


def run_cli(arguments: list[str], cwd: str, timeout: float = 900.0) -> CliResult | None:
    """Run the CLI to completion in ``cwd``. ``None`` means it could not be found or started.

    Synchronous on purpose. A build is a step in the middle of Play, and its failure has to be
    able to stop the launch -- fire-and-forget would put the error in a log while the runtime
    happily started against whatever the previous build left behind.
    """
    command = resolve_cli_command()
    if command is None:
        return None

    environment = dict(os.environ)
    ktx = _preference("ktx_path").strip()
    if ktx:
        # The CLI probes a vendored third_party/tools/KTX-Software, then PATH, then this. A
        # GUI-launched Blender usually has neither of the first two, and the failure is a build
        # that cannot encode a texture rather than one that says the tool is missing.
        environment["PARADISE_KTX_PATH"] = os.path.realpath(os.path.expanduser(ktx))

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


# --------------------------------------------------------------------------------------
# Launching the game
# --------------------------------------------------------------------------------------


def launch_runtime(arguments: list[str], cwd: str) -> tuple[subprocess.Popen | None, str | None]:
    """Launch the runtime detached. Returns ``(process, error message)``.

    The process is returned rather than its pid so a caller can ``poll()`` it. A detached child
    that dies immediately -- a missing asset, a bad argument -- is otherwise indistinguishable from
    one that launched fine, because its output went to :func:`log_path` and Blender only ever saw a
    successful fork.

    ``cwd`` is not optional in practice. A runtime resolves its own non-scene paths relative to the
    working directory, and a detached child inherits BLENDER's -- which is whatever the OS handed
    it. Started from a terminal that is the project root and everything works; started from the
    Dock it is ``/``, and the game dies looking for a config directory. The bug is invisible to
    whoever wrote the launcher and reproduces only for whoever launched Blender the other way.

    ONLY THE POSIX BRANCH REDIRECTS OUTPUT INTO :func:`log_path`, and that asymmetry is inherited
    from the module this was ported from rather than introduced here. The Windows branch passes
    ``creationflags`` and no handles, so nothing writes the log and :func:`first_error_line` has no
    file to read. Build failures still surface on Windows -- :func:`run_cli` captures those -- but
    a RUNTIME crash there reports only its exit code. Fixing it means handing the child its own
    handles, which is a small change nobody here can test; it is named rather than guessed at.
    """
    command = resolve_runtime_command()
    if command is None:
        return None, (
            "No runtime host is set. Point 'Runtime Host' in the addon preferences at the game's "
            "launcher -- an executable, or its .csproj to run via `dotnet run --project`."
        )

    argv = [*command, *arguments, *shlex.split(_preference("runtime_arguments"))]

    try:
        if os.name == "nt":
            process = subprocess.Popen(  # argv is built from resolved paths
                argv,
                cwd=cwd,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            # A GUI-launched Blender has a PATH without the dotnet directory, which `dotnet run`'s
            # child build steps need; and a detached child's output would otherwise vanish, so a
            # build error would manifest as "the window never appeared" with nothing to read.
            dotnet_dir = os.path.dirname(shutil.which("dotnet") or "/usr/local/share/dotnet/dotnet")
            quoted = " ".join(shlex.quote(a) for a in argv)
            script = f'export PATH="{dotnet_dir}:$PATH"; exec {quoted} > {shlex.quote(log_path())} 2>&1'
            process = subprocess.Popen(["/bin/sh", "-c", script], cwd=cwd, start_new_session=True)
    except OSError as error:
        return None, f"Failed to launch the runtime: {error}"

    return process, None


# Substrings that mark the line a failed run is actually about. "error" catches MSBuild
# (`error MSB3202: ...`), the exception words catch a .NET crash banner; a build log buries both
# under hundreds of lines and ends on a useless "The build failed."
_FAILURE_MARKERS = ("error", "exception", "unhandled", "fatal")


def first_error_line(path: str) -> str | None:
    """The most explanatory line of a failed run's log.

    Falls back to the FIRST line rather than the last: a launcher that fails its own precondition
    prints the cause first and a generic hint after it, so the tail is the least informative part.

    Build WARNINGS are skipped on that fallback, and skipping them is the whole point. A runtime
    that fails a precondition need not use any of the marker words -- "Could not find a part of the
    path 'data/game/config.json'" contains none -- so the fallback reports most real failures. And
    the first line of a `dotnet run` log is almost never the program's: it is whatever MSBuild
    warned about, which is both permanent and irrelevant. Reporting that back reads as the
    diagnosis and sends the reader off to fix an SDK warning that was never going to stop anything.
    """
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
    """A compiler or SDK warning, which a failed RUN is never explained by.

    Structured rather than a bare word search, and the distinction keeps a runtime's own output
    safe: every compiler and SDK warning is printed as ``<origin>: warning <CODE>: <text>``, so the
    colon is part of the shape. Matching the bare word would swallow a launcher's own fatal line if
    it happened to contain it -- precisely the line this exists to surface.
    """
    return ": warning " in line.lower()
