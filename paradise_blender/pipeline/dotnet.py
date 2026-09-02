"""Where ``dotnet`` is, and the environment every child that needs it runs in.

A Dock-launched macOS Blender inherits no shell profile, so ``dotnet`` is not on its PATH and
neither is it on any child's: the schema watcher's ``dotnet build``, the bridge's ``dotnet run``
and the play launcher all died on it, each with its own copy of the discovery list (#33). One
environment, built here, prepends the install directory so MSBuild's own ``<Exec Command="dotnet
exec …">`` steps resolve too, which a fully qualified argv[0] alone does not fix.
"""

from __future__ import annotations

import os
import shutil

__all__ = ["executable", "subprocess_environment"]

_WELL_KNOWN = (
    "/usr/local/share/dotnet/dotnet",
    "/opt/homebrew/bin/dotnet",
    os.path.expanduser("~/.dotnet/dotnet"),
    r"C:\Program Files\dotnet\dotnet.exe",
)


def executable() -> str | None:
    """PATH first, then the places a GUI-launched Blender's PATH misses."""
    found = shutil.which("dotnet")
    if found:
        return found
    return next((c for c in _WELL_KNOWN if os.path.exists(c)), None)


def subprocess_environment() -> dict[str, str]:
    """Blender's environment with the dotnet directory on PATH and the MSBuild server off (two
    concurrent ``dotnet`` builds sharing one die on MSB0001, which is what a bridge bake beside
    the schema watcher looked like)."""
    environment = dict(os.environ)
    dotnet = executable()
    if dotnet is not None:
        directory = os.path.dirname(os.path.realpath(dotnet))
        current = environment.get("PATH", "")
        if directory not in current.split(os.pathsep):
            environment["PATH"] = directory + os.pathsep + current if current else directory
    environment["DOTNET_CLI_DO_NOT_USE_MSBUILD_SERVER"] = "1"
    return environment
