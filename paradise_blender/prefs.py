"""Addon preferences: where data goes, and which external tools to drive.

The Godot host splits these between committed project settings and machine-level editor
settings. Blender's ``AddonPreferences`` are machine-level only, which is the right default
for tool paths (they differ per machine) but wrong for the data directory (it is a property of
the project). So the data directory is stored **per-scene** and the tool paths are stored in
preferences -- see :class:`ParadiseScenePreferences`.
"""

from __future__ import annotations

import os
import shutil

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import AddonPreferences, PropertyGroup, Scene

from .paths import ExportPaths

__all__ = [
    "ParadiseAddonPreferences",
    "ParadiseScenePreferences",
    "classes",
    "export_paths",
    "get_preferences",
    "resolve_blender_data_dir",
]

PACKAGE = __package__


def _update_navmesh_preview(_self, context) -> None:
    # Deferred import: prefs is imported by nearly every module, and importing the preview
    # module here at load time would create a cycle through export/.
    #
    # Defined ABOVE the class and referenced by name: `from __future__ import annotations`
    # turns the property definitions into strings that Blender re-evaluates, and a lambda
    # compiled from that string loses the module globals — it raises NameError at call time.
    from .export.navmesh_preview import sync_preview_visibility

    sync_preview_visibility(context.scene)


class ParadiseScenePreferences(PropertyGroup):
    """Project-scoped settings, stored in the .blend so they travel with the scene."""

    data_dir: StringProperty(  # type: ignore[valid-type]
        name="Data Directory",
        description=(
            "Export output root. The runtime resolves every contract path under this "
            "directory, so assets outside it are unreachable at runtime"
        ),
        subtype="DIR_PATH",
        default="//data",
    )

    export_on_save: BoolProperty(  # type: ignore[valid-type]
        name="Export On Save",
        description=(
            "Re-export the scene contract when the .blend is saved, matching the Godot host's "
            "save hook"
        ),
        default=True,
    )

    scene_name_override: StringProperty(  # type: ignore[valid-type]
        name="Scene Name",
        description=(
            "Output name for data/scenes/<name>.json. Empty uses the .blend filename, "
            "matching the Godot host's rule of using the scene file's basename"
        ),
        default="",
    )

    navmesh_preview: BoolProperty(  # type: ignore[valid-type]
        name="Preview NavMesh",
        description=(
            "Show the last baked navmesh as a wireframe overlay in the viewport. Bake NavMesh "
            "(re)builds the overlay from the actual bake output, so erosion and doorway cuts "
            "are what the runtime will see"
        ),
        default=False,
        update=_update_navmesh_preview,
    )

    # -- lighting ---------------------------------------------------------------------------
    #
    # Scene-scoped for the same reason the navmesh parameters are: it shapes exported data
    # (the contract's Lighting.ShadowMapSize), so it must travel inside the .blend.

    shadow_map_size: EnumProperty(  # type: ignore[valid-type]
        name="Shadow Map",
        description=(
            "Per-layer shadow map resolution the runtime allocates for this scene. Higher "
            "sharpens shadow edges and costs GPU memory; the sun's coverage area is fixed "
            "around the camera, so this trades texel density directly"
        ),
        items=(
            ("DEFAULT", "Engine Default", "Leave the renderer's built-in resolution"),
            ("512", "512", "512 x 512 per shadow layer"),
            ("1024", "1024", "1024 x 1024 per shadow layer"),
            ("2048", "2048", "2048 x 2048 per shadow layer"),
            ("4096", "4096", "4096 x 4096 per shadow layer"),
        ),
        default="DEFAULT",
    )

    shadow_blur: FloatProperty(  # type: ignore[valid-type]
        name="Shadow Blur",
        description=(
            "Soft-shadow penumbra width, in shadow-map texels (the runtime's PCF disk "
            "radius). Below ~2 the map's texel staircase shows through the filter; large "
            "values detach contact shadows into mush"
        ),
        default=3.0,
        min=0.5,
        max=8.0,
    )

    # -- navmesh bake parameters ------------------------------------------------------------
    #
    # Scene-scoped, not preferences: they shape EXPORTED DATA (the .navmesh.bin every checkout
    # of this project loads), so they must travel inside the .blend like the data directory
    # does. Defaults mirror the Godot host's NavMeshBake.cs — a scene authored with default
    # settings bakes identically from either tool.

    navmesh_cell_size: FloatProperty(  # type: ignore[valid-type]
        name="Cell Size",
        description=(
            "Voxel size on the ground plane. Smaller hugs walls and doorways more precisely "
            "and bakes slower; it must stay well under the narrowest passage or Recast cannot "
            "see through it"
        ),
        default=0.1,
        min=0.01,
        max=1.0,
        subtype="DISTANCE",
    )

    navmesh_cell_height: FloatProperty(  # type: ignore[valid-type]
        name="Cell Height",
        description=(
            "Voxel size vertically. Governs how precisely ledges and climb heights are "
            "resolved; keep it at or below Max Climb"
        ),
        default=0.1,
        min=0.01,
        max=1.0,
        subtype="DISTANCE",
    )

    navmesh_agent_radius: FloatProperty(  # type: ignore[valid-type]
        name="Agent Radius",
        description=(
            "Walkable area is eroded by this much. At 0 planned paths run flush against "
            "obstacle faces and agent capsules grind along walls — match the game's largest "
            "agent capsule"
        ),
        default=0.4,
        min=0.0,
        max=2.0,
        subtype="DISTANCE",
    )

    navmesh_agent_height: FloatProperty(  # type: ignore[valid-type]
        name="Agent Height",
        description=(
            "Minimum vertical clearance a surface needs to count as walkable — what stops "
            "paths from routing under low geometry"
        ),
        default=1.8,
        min=0.1,
        max=5.0,
        subtype="DISTANCE",
    )

    navmesh_agent_max_climb: FloatProperty(  # type: ignore[valid-type]
        name="Max Climb",
        description=(
            "Highest step an agent can walk up. Surfaces this far apart vertically merge into "
            "one walkable region — a raised platform below this height is reachable without a "
            "ramp"
        ),
        default=0.3,
        min=0.0,
        max=2.0,
        subtype="DISTANCE",
    )

    # No ANGLE subtype: that displays radians-backed values, while Recast takes plain degrees.
    navmesh_agent_max_slope: FloatProperty(  # type: ignore[valid-type]
        name="Max Slope (deg)",
        description="Steepest surface angle in degrees that still counts as walkable",
        default=45.0,
        min=0.0,
        max=89.0,
    )


class ParadiseAddonPreferences(AddonPreferences):
    """Machine-scoped settings: external tool locations and live-preview transport."""

    bl_idname = PACKAGE

    runtime_host: StringProperty(  # type: ignore[valid-type]
        name="Runtime Host",
        description=(
            "Path to a paradise-runtime executable, or to a host .csproj (launched via "
            "`dotnet run --project`). Empty auto-detects the installed dotnet tool"
        ),
        subtype="FILE_PATH",
        default="",
    )

    runtime_arguments: StringProperty(  # type: ignore[valid-type]
        name="Runtime Arguments",
        description="Extra arguments appended to every runtime launch",
        default="--imgui",
    )

    ktx_path: StringProperty(  # type: ignore[valid-type]
        name="KTX Tool Path",
        description=(
            "KTX-Software's `ktx` (4.4+) or the legacy `toktx`, used to transcode GLB textures "
            "to KTX2. Either is accepted; the dialect is chosen from the filename. Without one, "
            "exports still succeed, but textured meshes will not load in the runtime, which "
            "requires KTX2"
        ),
        subtype="FILE_PATH",
        default="",
    )

    bridge_project: StringProperty(  # type: ignore[valid-type]
        name="Bridge Project",
        description=(
            "tools/ParadiseBlenderBridge .csproj, used for navmesh baking and the contract "
            "conformance check. Only needed for those two operations"
        ),
        subtype="FILE_PATH",
        default="",
    )

    live_port: IntProperty(  # type: ignore[valid-type]
        name="Live Preview Port",
        description="Loopback TCP port the runtime listens on for live-preview updates",
        default=45123,
        min=1024,
        max=65535,
    )

    live_rate_hz: IntProperty(  # type: ignore[valid-type]
        name="Live Update Rate",
        description=(
            "How often coalesced edits are pushed. Blender fires depsgraph updates far faster "
            "than this; sending each one would flood the socket while dragging"
        ),
        default=10,
        min=1,
        max=60,
    )

    def draw(self, context) -> None:
        layout = self.layout

        box = layout.box()
        box.label(text="Runtime", icon="PLAY")
        box.prop(self, "runtime_host")
        box.prop(self, "runtime_arguments")
        resolved = shutil.which("paradise-runtime") or _dotnet_tool_path()
        if not self.runtime_host and resolved:
            box.label(text=f"Auto-detected: {resolved}", icon="CHECKMARK")
        elif not self.runtime_host:
            box.label(
                text="No runtime found. Install with: dotnet tool install --global Paradise.Sample.Runtime",
                icon="ERROR",
            )

        box = layout.box()
        box.label(text="Live Preview", icon="LINKED")
        box.prop(self, "live_port")
        box.prop(self, "live_rate_hz")

        box = layout.box()
        box.label(text="Optional Tools", icon="TOOL_SETTINGS")
        box.prop(self, "ktx_path")
        box.prop(self, "bridge_project")


def _dotnet_tool_path() -> str | None:
    candidate = os.path.join(
        os.path.expanduser("~"),
        ".dotnet",
        "tools",
        "paradise-runtime.exe" if os.name == "nt" else "paradise-runtime",
    )
    return candidate if os.path.exists(candidate) else None


def get_preferences(context=None) -> ParadiseAddonPreferences:
    context = context or bpy.context
    return context.preferences.addons[PACKAGE].preferences


def resolve_blender_data_dir(scene: bpy.types.Scene) -> str:
    """Absolute path of the scene's data directory.

    Blender's ``//`` prefix is relative to the .blend file, so an unsaved file cannot resolve
    it. Rather than silently writing into the current working directory -- which is wherever
    Blender happened to be launched from -- this falls back to a temp directory and the
    exporter reports where it went.
    """
    raw = scene.paradise_project.data_dir or "//data"
    if raw.startswith("//") and not bpy.data.filepath:
        import tempfile

        return os.path.join(tempfile.gettempdir(), "paradise_unsaved_blend", "data")
    return os.path.abspath(bpy.path.abspath(raw))


def export_paths(scene: bpy.types.Scene) -> ExportPaths:
    return ExportPaths(resolve_blender_data_dir(scene))


classes = (ParadiseScenePreferences, ParadiseAddonPreferences)


def register_pointers() -> None:
    from bpy.props import PointerProperty

    Scene.paradise_project = PointerProperty(type=ParadiseScenePreferences)


def unregister_pointers() -> None:
    if hasattr(Scene, "paradise_project"):
        del Scene.paradise_project
