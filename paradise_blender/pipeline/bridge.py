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

__all__ = ["bridge_identity", "resolve_bridge_command"]


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


def bridge_identity(command: list[str]) -> str:
    """A string that changes when the bridge that would run changes -- for cache keys.

    Callers cache the bridge's *output* (a navmesh bake) against a hash of its *input*, which is
    only sound while the bridge itself is fixed. This adds the bridge to that key: the argv, plus
    the size and mtime of every assembly in its build output.

    **Every assembly, not just the bridge's own**, because the bake is Recast running inside
    ``Paradise.Export`` — which sits in that same directory as a dependency. In a workspace that
    compiles the engine from source (see the root ``Directory.Build.targets``), a change to the
    bake belongs to the engine, and keying on ``ParadiseBlenderBridge.dll`` alone would reuse a
    navmesh baked by the previous engine. Stats rather than content hashes: a rebuild is the
    signal, and one unnecessary re-bake is the right direction to be wrong in.

    Its limit is worth stating plainly, because the failure is silent. The assemblies are the
    ones from the LAST build, so editing engine source and re-exporting *without building in
    between* leaves the identity unchanged and reuses the old bake. Building moves it. The
    panel's Bake NavMesh never consults the cache, so it is always the honest answer.
    """
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
            # Vanished between listing and stat (a build running concurrently). Skipping it only
            # makes the identity coarser, which costs a re-bake at worst.
            continue


def _executed_assembly(command: list[str]) -> str | None:
    """The file whose contents decide what the bridge does: the published binary, or the DLL
    ``dotnet run`` would launch from the project's most recent build output."""
    if "--project" not in command:
        return command[0] if command else None

    project = command[command.index("--project") + 1]
    build_root = os.path.join(os.path.dirname(project), "bin")
    name = os.path.splitext(os.path.basename(project))[0] + ".dll"
    candidates = [
        os.path.join(root, name) for root, _dirs, files in os.walk(build_root) if name in files
    ]
    # Newest wins. `dotnet run` launches the Debug build unless told otherwise, but Debug and
    # Release outputs coexist and a rebuild of either says the bridge may have moved.
    return max(candidates, key=os.path.getmtime) if candidates else None


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
