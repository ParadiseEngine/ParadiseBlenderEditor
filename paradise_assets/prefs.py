"""Machine-scoped toolchain paths. "Which launcher runs this game" would belong in
``project.toml``, but the manifest loader is strict and an addon key there would fail the build.
"""

from __future__ import annotations

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import AddonPreferences

__all__ = ["PACKAGE", "ParadiseAssetsPreferences", "classes", "get_preferences"]

#: An extension's module is ``bl_ext.<repo>.paradise_assets``; only ``__package__`` knows the repo.
PACKAGE = __package__


class ParadiseAssetsPreferences(AddonPreferences):
    """Where the asset CLI, the game runtime and the texture encoder are."""

    bl_idname = PACKAGE

    cli: StringProperty(  # type: ignore[valid-type]
        name="Paradise CLI",
        description=(
            "Path to the `paradise` executable, or to Paradise.Cli.csproj (run via "
            "`dotnet run --project`). Empty looks on PATH and then for the installed dotnet tool"
        ),
        subtype="FILE_PATH",
        default="",
    )

    runtime_host: StringProperty(  # type: ignore[valid-type]
        name="Runtime Host",
        description=(
            "The game's launcher: an executable, or a .csproj run via `dotnet run --project`. "
            "This is the game's, not the engine's -- there is no default that could be right"
        ),
        subtype="FILE_PATH",
        default="",
    )

    runtime_arguments: StringProperty(  # type: ignore[valid-type]
        name="Runtime Arguments",
        description=(
            "Extra arguments appended to every launch. The addon passes --scene itself; "
            "anything game-specific (--config, --ui, --seed) belongs here"
        ),
        default="",
    )

    build_profile: StringProperty(  # type: ignore[valid-type]
        name="Build Profile",
        description=(
            "Which [build.profiles.*] in assets/project.toml a Play build uses. The project "
            "invents its own profile names, so this is text rather than a menu"
        ),
        default="dev",
    )

    ktx_path: StringProperty(  # type: ignore[valid-type]
        name="KTX Executable",
        description=(
            "The ktx binary itself (KTX-Software v5), e.g. .../KTX-Software/bin/ktx.exe — not "
            "the directory holding it. Exported to the build as PARADISE_KTX_PATH"
        ),
        # FILE_PATH: the pipeline checks File.Exists, so a directory looks configured and
        # behaves exactly like blank.
        subtype="FILE_PATH",
        default="",
    )

    mirror_model_prefabs: BoolProperty(  # type: ignore[valid-type]
        name="Keep a Prefab per Model",
        description=(
            "While a document is open, create a prefab under prefabs/models/ for every model "
            "that has none, follow the model when it moves, and DELETE the prefab when the "
            "model is gone. Off by default because of that last one -- a hand-authored prefab "
            "is never touched, but a generated one is this mirror's to remove"
        ),
        # Destructive behaviour ships off: nothing an addon update turns on should be able to
        # delete a file under an author who never asked for it.
        default=False,
    )

    static_mesh_component: StringProperty(  # type: ignore[valid-type]
        name="Static Mesh Component",
        description=(
            "Which component a generated prefab authors an unrigged model's mesh into, by CLR "
            "type name. The game decides how many mesh-bearing components it declares, so this "
            "cannot be guessed; leave it empty when the schema declares exactly one"
        ),
        default="",
    )

    skinned_mesh_component: StringProperty(  # type: ignore[valid-type]
        name="Skinned Mesh Component",
        description=(
            "The same, for a model that carries a rig. A rigged model authored as static is a "
            "prefab that loads, shows the mesh, and is the wrong kind of thing in the game -- so "
            "the mirror stays idle on rigged models until this is set. A project with none does "
            "not need it"
        ),
        default="",
    )

    auto_watch: BoolProperty(  # type: ignore[valid-type]
        name="Watch While a Document Is Open",
        description=(
            "Start `paradise assets watch` for the project when a prefab or its cached .blend "
            "is opened, so edits reach the build without starting one by hand. One watcher per "
            "project, stopped when Blender quits or opens another file. Turn this off if you "
            "run your own"
        ),
        default=True,
    )

    def draw(self, _context) -> None:
        layout = self.layout

        box = layout.box()
        box.label(text="Build", icon="TOOL_SETTINGS")
        box.prop(self, "cli")
        box.prop(self, "build_profile")
        box.prop(self, "ktx_path")
        box.prop(self, "auto_watch")
        box.prop(self, "mirror_model_prefabs")

        box = layout.box()
        box.label(text="Play", icon="PLAY")
        box.prop(self, "runtime_host")
        box.prop(self, "runtime_arguments")


def get_preferences(context=None):
    """The preferences, or ``None`` when not registered (integration tests import directly)."""
    context = context or bpy.context
    addon = context.preferences.addons.get(PACKAGE)
    return addon.preferences if addon is not None else None


classes = (ParadiseAssetsPreferences,)
