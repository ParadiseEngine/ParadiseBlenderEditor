"""Locating and launching the standalone runtime (port of ``ParadiseExportPlugin.OnPlayDotnet``).

Play never exports: ``data/`` is kept fresh by the save hook, and a Play that exported would
silently rewrite assets. On POSIX the child is wrapped in a shell that fixes PATH (a GUI-launched
Blender has no dotnet directory on it) and redirects to a log (a detached child's output would
otherwise vanish, so a build error reads as "the window never appeared").
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
    """Argv prefix for the runtime, or ``None``. ``warn=False`` for the panel, which resolves
    on every redraw."""
    from ..prefs import get_preferences

    try:
        configured = get_preferences().runtime_host.strip()
    except (KeyError, AttributeError):
        configured = ""

    if configured:
        # realpath, not abspath: MSBuild resolves ProjectReferences against the canonical
        # directory, so a symlinked csproj dies on MSB3202, and Blender's file browser walks
        # into symlinks happily.
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
    """Launch the runtime detached; returns the process (so a caller can ``poll()`` an immediate
    death that would otherwise look like a clean launch), or ``None``.

    ``cwd`` is required: a detached child inherits Blender's working directory, ``/`` when
    launched from the Dock, and the runtime dies looking for ``/data/<game>/config.json``. Only
    the POSIX branch writes the log; a Windows runtime crash reports only its exit code.
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

    match = next(
        (line for line in lines if any(m in line.lower() for m in _FAILURE_MARKERS)),
        None,
    )
    if match is None:
        # A wrong answer beats no answer when the log is nothing but noise.
        match = next((line for line in lines if not _is_build_noise(line)), lines[0])
    return match if len(match) <= 300 else match[:297] + "..."


def _is_build_noise(line: str) -> bool:
    """A compiler/SDK warning (``<origin>: warning <CODE>:``). Shape-matched, not a bare word
    search, or a launcher's own fatal line containing "warning" would be swallowed."""
    return ": warning " in line.lower()


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
