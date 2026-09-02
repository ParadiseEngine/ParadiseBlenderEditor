"""Locating and invoking the .NET bridge (``navmesh`` bake, ``contract-check``), the two jobs
Python cannot do. Opt-in: a missing bridge degrades them with a warning. KNOWN GAP (#33): the
launch has no PATH fix, so it fails from a Dock-launched macOS Blender.
"""

from __future__ import annotations

import os
import shutil

__all__ = ["bridge_identity", "resolve_bridge_command"]


def resolve_bridge_command() -> list[str] | None:
    """Argv prefix for the bridge (preference, PATH, then this repo's project), or ``None``."""
    from ..prefs import get_preferences

    try:
        configured = get_preferences().bridge_project.strip()
    except (KeyError, AttributeError):
        # No preferences when headless without the addon registered (integration tests).
        configured = ""

    if configured:
        resolved = os.path.abspath(_expand(configured))
        if resolved.endswith(".csproj"):
            return _dotnet_run(resolved)
        if os.path.exists(resolved):
            return [resolved]

    on_path = shutil.which("ParadiseBlenderBridge")
    if on_path:
        return [on_path]

    repo_project = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "tools",
        "ParadiseBlenderBridge",
        "ParadiseBlenderBridge.csproj",
    )
    if os.path.exists(repo_project):
        return _dotnet_run(repo_project)

    return None


def bridge_identity(command: list[str]) -> str:
    """A cache-key component that changes when the bridge that would run changes: the argv plus
    the stats of EVERY assembly in its build output, since the bake runs inside ``Paradise.Export``
    and a source-override workspace changes that without touching the bridge's own DLL. Limit:
    engine source edited but not rebuilt leaves the identity unchanged and reuses the old bake;
    the panel's Bake NavMesh never consults the cache."""
    binary = _executed_assembly(command)
    if binary is None:
        return " ".join(command)

    directory = os.path.dirname(binary)
    try:
        assemblies = sorted(n for n in os.listdir(directory) if n.endswith((".dll", ".exe")))
        stats = [f"{name}:{s.st_size}:{int(s.st_mtime)}" for name, s in _stat_all(directory, assemblies)]
    except OSError:
        return " ".join(command)

    return " ".join([*command, *stats])


def _stat_all(directory: str, names: list[str]):
    for name in names:
        try:
            yield name, os.stat(os.path.join(directory, name))
        except OSError:
            # Vanished under a concurrent build; a coarser identity costs a re-bake at worst.
            continue


def _executed_assembly(command: list[str]) -> str | None:
    """The binary that would run: the published one, or the newest build output's DLL."""
    if "--project" not in command:
        return command[0] if command else None

    project = command[command.index("--project") + 1]
    build_root = os.path.join(os.path.dirname(project), "bin")
    name = os.path.splitext(os.path.basename(project))[0] + ".dll"
    candidates = [
        os.path.join(root, name) for root, _dirs, files in os.walk(build_root) if name in files
    ]
    # Newest wins: Debug and Release coexist and a rebuild of either may have moved the bridge.
    return max(candidates, key=os.path.getmtime) if candidates else None


def _dotnet_run(project: str) -> list[str] | None:
    dotnet = shutil.which("dotnet") or _well_known_dotnet()
    if dotnet is None:
        return None
    # `--`, or verbs like `navmesh` parse as dotnet options.
    return [dotnet, "run", "--project", project, "--"]


def _well_known_dotnet() -> str | None:
    """Blender's PATH often lacks the dotnet install directory when launched from a GUI."""
    candidates = [
        "/usr/local/share/dotnet/dotnet",
        "/opt/homebrew/bin/dotnet",
        os.path.expanduser("~/.dotnet/dotnet"),
        r"C:\Program Files\dotnet\dotnet.exe",
    ]
    return next((c for c in candidates if os.path.exists(c)), None)


def _expand(path: str) -> str:
    import bpy

    return bpy.path.abspath(path) if path.startswith("//") else os.path.expanduser(path)
