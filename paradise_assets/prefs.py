"""Machine-scoped settings: where the toolchain lives.

All of it is about EXTERNAL TOOLS, and none of it belongs in the project. ``assets/project.toml``
would be the natural home for "which launcher runs this game", but its loading is strict by design
-- ``ProjectManifest``'s own remarks say a value the build does not understand is an error naming
the key -- so an addon-invented key there would fail the CLI's build rather than be ignored. That
makes these preferences rather than project settings until the manifest has somewhere to put them.

Machine-scoped is right for the rest regardless: a path to a .NET SDK project, a KTX install and a
CLI build are facts about this computer, not about the game, and committing them would hand every
teammate someone else's directory layout.
"""

from __future__ import annotations

import bpy
from bpy.props import StringProperty
from bpy.types import AddonPreferences

__all__ = ["PACKAGE", "ParadiseAssetsPreferences", "classes", "get_preferences"]

#: This addon is installed as an extension, so the module is ``bl_ext.<repo>.paradise_assets`` and
#: the repo name is whatever the user called it. ``__package__`` is the only thing that knows.
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
        name="KTX-Software",
        description=(
            "Directory holding the KTX tools, exported to the build as PARADISE_KTX_PATH. "
            "Without it a build that needs to encode a texture cannot, and says so"
        ),
        subtype="DIR_PATH",
        default="",
    )

    def draw(self, _context) -> None:
        layout = self.layout

        box = layout.box()
        box.label(text="Build", icon="TOOL_SETTINGS")
        box.prop(self, "cli")
        box.prop(self, "build_profile")
        box.prop(self, "ktx_path")

        box = layout.box()
        box.label(text="Play", icon="PLAY")
        box.prop(self, "runtime_host")
        box.prop(self, "runtime_arguments")


def get_preferences(context=None):
    """This addon's preferences, or ``None`` when it is not registered as an addon.

    ``None`` rather than an exception: the integration tests import these modules directly rather
    than installing the extension, and every caller here already has a "not configured" path to
    fall into.
    """
    context = context or bpy.context
    addon = context.preferences.addons.get(PACKAGE)
    return addon.preferences if addon is not None else None


classes = (ParadiseAssetsPreferences,)
