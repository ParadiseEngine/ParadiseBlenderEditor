"""Machine-scoped toolchain paths. "Which launcher runs this game" is the project's, not the
machine's: ``[host]`` in ``assets/project.toml``, read by ``paradise host play``.
"""

from __future__ import annotations

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import AddonPreferences

__all__ = ["PACKAGE", "ParadiseAssetsPreferences", "classes", "get_preferences"]

#: An extension's module is ``bl_ext.<repo>.paradise_assets``; only ``__package__`` knows the repo.
PACKAGE = __package__


class ParadiseAssetsPreferences(AddonPreferences):
    """Where the asset CLI and the texture encoder are. The game's launcher is NOT here: it is
    ``[host]`` in the project's ``assets/project.toml``, so a script and CI run the same game the
    same way (ParadiseEngine's ``paradise host play``)."""

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


def get_preferences(context=None):
    """The preferences, or ``None`` when not registered (integration tests import directly)."""
    context = context or bpy.context
    addon = context.preferences.addons.get(PACKAGE)
    return addon.preferences if addon is not None else None


classes = (ParadiseAssetsPreferences,)
