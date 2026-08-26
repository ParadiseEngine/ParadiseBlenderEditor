"""Locating and launching the standalone Paradise runtime.

Port of ``ParadiseExportPlugin.ResolveRuntimeHostCommand`` / ``OnPlayDotnet``, including two
non-obvious behaviours inherited from that host because they solve real problems:

* **Launch is a pure consumer of ``data/``.** Play never exports. The data directory is
  authoring output kept fresh by the save hook and the export operator; making Play export too
  would mean the button silently rewrites assets, and a slow export would look like a slow
  launch.

* **On POSIX the child is wrapped in a shell.** Two realities force it: a GUI-launched Blender
  has a PATH without the dotnet directory (which ``dotnet run``'s child build steps need), and
  a detached child's output would otherwise vanish -- so build errors would manifest as "the
  window never appeared" with nothing to read. The wrapper fixes PATH and redirects to a log.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile

from .. import log

__all__ = ["first_error_line", "launch_runtime", "log_path", "resolve_runtime_command"]


def log_path() -> str:
    return os.path.join(tempfile.gettempdir(), "paradise_play.log")


def resolve_runtime_command(warn: bool = True) -> list[str] | None:
    """Argv prefix that runs the runtime, or ``None`` when nothing is installed.

    Order: the configured host (a ``.csproj`` runs via ``dotnet run``), then
    ``paradise-runtime`` on PATH, then the globally installed dotnet tool.

    ``warn=False`` silences the diagnostics. The Play panel resolves on every redraw to show
    the current status, and an unconditional warn there would print once per redraw.
    """
    from ..prefs import get_preferences

    try:
        configured = get_preferences().runtime_host.strip()
    except (KeyError, AttributeError):
        configured = ""

    if configured:
        # realpath, not abspath: a project reached through a symlink builds against the wrong
        # tree. MSBuild relativizes ProjectReferences against the path as spelled, then resolves
        # them against the canonicalized directory -- so a symlinked project path silently
        # retargets every relative reference and the build dies on MSB3202. Blender's file
        # browser walks into symlinks happily, so authors hit this by simply picking the file.
        resolved = os.path.realpath(os.path.expanduser(configured))
        if resolved.endswith(".csproj"):
            command = _dotnet_run(resolved, warn=warn)
            if command is not None:
                return command
        elif os.path.exists(resolved):
            return [resolved]
        elif warn:
            log.warn(f"Configured runtime host '{configured}' does not exist; auto-detecting.")

    on_path = shutil.which("paradise-runtime")
    if on_path:
        return [on_path]

    tool = os.path.join(
        os.path.expanduser("~"),
        ".dotnet",
        "tools",
        "paradise-runtime.exe" if os.name == "nt" else "paradise-runtime",
    )
    if os.path.exists(tool):
        return [tool]

    return None


def launch_runtime(
    arguments: list[str], operator=None, cwd: str | None = None
) -> subprocess.Popen | None:
    """Launch the runtime detached with the given arguments. Returns the process, or ``None``.

    Detached on purpose: the runtime is an interactive window with its own lifetime, and
    Blender must not block or die with it.

    The process is returned rather than its pid so callers can ``poll()`` it. A detached child
    that dies immediately -- a build error, a missing asset -- is otherwise indistinguishable
    from one that launched fine, because its output went to :func:`log_path` and Blender only
    ever saw a successful ``fork``.

    ``cwd`` is the directory to run IN, and passing it is not optional in practice. A runtime
    resolves its own non-scene paths relative to the working directory -- ShiningPie's launcher
    reads ``data/<game>/config.json`` and ``ui/GameShell.xaml`` that way, and says so when it
    fails -- and a detached child inherits BLENDER's working directory, which is whatever the OS
    handed it. Started from a terminal that is the project root and everything works; started
    from Finder or the Dock it is ``/``, and the runtime dies looking for
    ``/data/<game>/config.json``. The bug is invisible to whoever wrote the launcher and
    reproduces only for whoever launched Blender the other way.
    """
    command = resolve_runtime_command()
    if command is None:
        log.error(
            "No Paradise runtime found. Set a runtime host in the Play panel -- an executable, "
            "or a host .csproj to run via `dotnet run --project`.",
            operator,
        )
        return None

    from ..prefs import get_preferences

    try:
        extra = shlex.split(get_preferences().runtime_arguments)
    except (KeyError, AttributeError):
        extra = []

    argv = [*command, *arguments, *extra]

    try:
        if os.name == "nt":
            process = subprocess.Popen(  # argv is built from resolved paths
                argv,
                cwd=cwd,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            dotnet_dir = os.path.dirname(shutil.which("dotnet") or "/usr/local/share/dotnet/dotnet")
            quoted = " ".join(shlex.quote(a) for a in argv)
            script = f'export PATH="{dotnet_dir}:$PATH"; exec {quoted} > {shlex.quote(log_path())} 2>&1'
            process = subprocess.Popen(
                ["/bin/sh", "-c", script],
                cwd=cwd,
                start_new_session=True,
            )
    except OSError as error:
        log.error(f"Failed to launch the runtime: {error}", operator)
        return None

    log.info(f"Launched Paradise runtime (pid {process.pid}) — output: {log_path()}", operator)
    return process


# Substrings that mark the line a failed run is actually about. "error" catches MSBuild
# (`error MSB3202: ...`), the exception words catch a .NET crash banner; a build log buries both
# under hundreds of lines and ends on a useless "The build failed."
_FAILURE_MARKERS = ("error", "exception", "unhandled", "fatal")


def first_error_line(path: str) -> str | None:
    """The most explanatory line of a failed run's log, for reporting back into Blender.

    Falls back to the *first* line rather than the last: a launcher that fails its own
    precondition check prints the cause first and a generic hint after it, so the tail is the
    least informative part. Anything that fails later, after real output, is caught by a marker.

    <b>Build WARNINGS are skipped on that fallback, and skipping them is the whole point.</b> A
    runtime that fails a precondition need not use any of the words in ``_FAILURE_MARKERS`` --
    "Could not find a part of the path 'data/game/config.json'" contains none of them -- so the
    fallback is what actually reports most real failures. And the first line of a ``dotnet run``
    log is almost never the program's: it is whatever MSBuild warned about, which is both
    permanent and irrelevant. Reporting that back reads as the diagnosis and sends the reader off
    to fix an SDK warning that was never going to stop anything.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = [line.strip() for line in handle if line.strip()]
    except OSError:
        return None

    if not lines:
        return None

    match = next(
        (line for line in lines if any(m in line.lower() for m in _FAILURE_MARKERS)),
        None,
    )
    if match is None:
        # Nothing named itself an error, so take the first line that is not build noise -- and
        # only fall back to the true first line if the log is nothing BUT noise, where a wrong
        # answer is better than no answer.
        match = next((line for line in lines if not _is_build_noise(line)), lines[0])
    return match if len(match) <= 300 else match[:297] + "..."


def _is_build_noise(line: str) -> bool:
    """A compiler or SDK warning, which a failed RUN is never explained by.

    Deliberately narrow: it matches the word "warning", not "did this line come from MSBuild".
    A build that actually fails emits "error", which ``_FAILURE_MARKERS`` catches before this is
    ever consulted.
    """
    return "warning" in line.lower()


def _dotnet_run(project: str, warn: bool = True) -> list[str] | None:
    if not os.path.exists(project):
        if warn:
            log.warn(f"Configured runtime project '{project}' does not exist; auto-detecting.")
        return None
    dotnet = shutil.which("dotnet") or _well_known_dotnet()
    if dotnet is None:
        if warn:
            log.warn("The configured runtime host is a .csproj but the .NET SDK was not found.")
        return None
    return [dotnet, "run", "--project", project, "--"]


def _well_known_dotnet() -> str | None:
    candidates = [
        "/usr/local/share/dotnet/dotnet",
        "/opt/homebrew/bin/dotnet",
        os.path.expanduser("~/.dotnet/dotnet"),
        r"C:\Program Files\dotnet\dotnet.exe",
    ]
    return next((c for c in candidates if os.path.exists(c)), None)
