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

__all__ = ["launch_runtime", "log_path", "resolve_runtime_command"]


def log_path() -> str:
    return os.path.join(tempfile.gettempdir(), "paradise_play.log")


def resolve_runtime_command() -> list[str] | None:
    """Argv prefix that runs the runtime, or ``None`` when nothing is installed.

    Order: the configured host (a ``.csproj`` runs via ``dotnet run``), then
    ``paradise-runtime`` on PATH, then the globally installed dotnet tool.
    """
    from ..prefs import get_preferences

    try:
        configured = get_preferences().runtime_host.strip()
    except (KeyError, AttributeError):
        configured = ""

    if configured:
        resolved = os.path.abspath(os.path.expanduser(configured))
        if resolved.endswith(".csproj"):
            command = _dotnet_run(resolved)
            if command is not None:
                return command
        elif os.path.exists(resolved):
            return [resolved]
        else:
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


def launch_runtime(arguments: list[str], operator=None) -> int | None:
    """Launch the runtime detached with the given arguments. Returns its pid, or ``None``.

    Detached on purpose: the runtime is an interactive window with its own lifetime, and
    Blender must not block or die with it.
    """
    command = resolve_runtime_command()
    if command is None:
        log.error(
            "No Paradise runtime found. Install it with "
            "`dotnet tool install --global Paradise.Sample.Runtime`, or set a runtime host in "
            "the addon preferences.",
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
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            dotnet_dir = os.path.dirname(shutil.which("dotnet") or "/usr/local/share/dotnet/dotnet")
            quoted = " ".join(shlex.quote(a) for a in argv)
            script = f'export PATH="{dotnet_dir}:$PATH"; exec {quoted} > {shlex.quote(log_path())} 2>&1'
            process = subprocess.Popen(
                ["/bin/sh", "-c", script],
                start_new_session=True,
            )
    except OSError as error:
        log.error(f"Failed to launch the runtime: {error}", operator)
        return None

    log.info(f"Launched Paradise runtime (pid {process.pid}) — output: {log_path()}", operator)
    return process.pid


def _dotnet_run(project: str) -> list[str] | None:
    dotnet = shutil.which("dotnet") or _well_known_dotnet()
    if dotnet is None:
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
