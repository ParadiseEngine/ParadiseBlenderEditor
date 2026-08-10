"""Locating and invoking the .NET bridge CLI.

``tools/ParadiseBlenderBridge`` exists for the two jobs Python cannot do:

* ``navmesh`` -- Recast/Detour baking, because DotRecast is C# only
* ``contract-check`` -- round-tripping our JSON through the engine's own
  ``ExportJsonReader``/``ExportJsonWriter``, which is the only way to prove the Python contract
  implementation has not drifted from the C# one

Both are opt-in: ordinary export, play, and live preview never touch .NET, which is why the
contract writer is pure Python in the first place. A missing bridge degrades those two features
with a warning rather than breaking the addon.
"""

from __future__ import annotations

import os
import shutil

__all__ = ["resolve_bridge_command"]


def resolve_bridge_command() -> list[str] | None:
    """Argv prefix that runs the bridge, or ``None`` if it cannot be found.

    Resolution order:

    1. the explicit path in addon preferences (a ``.csproj`` runs via ``dotnet run``)
    2. a ``ParadiseBlenderBridge`` executable on PATH (a published build)
    3. the ``tools/ParadiseBlenderBridge`` project inside this repo -- the dev-workbench case

    ``dotnet run`` builds on demand, so the first navmesh bake after a code change takes a few
    seconds before it produces anything.
    """
    from ..prefs import get_preferences

    try:
        configured = get_preferences().bridge_project.strip()
    except (KeyError, AttributeError):
        # Preferences are unavailable when running headless without the addon registered
        # (the integration tests do this); fall through to the other candidates.
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


def _dotnet_run(project: str) -> list[str] | None:
    dotnet = shutil.which("dotnet") or _well_known_dotnet()
    if dotnet is None:
        return None
    # `--` separates dotnet's own arguments from the program's, or verbs like `navmesh` would
    # be parsed as dotnet options.
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
