"""Environment parameters Blender has no honest source for (Godot's sky gradient, SSAO, glow,
fog), authored explicitly. Tone mapping, background and sun are read from Blender instead and
are deliberately absent here. The sky fields mirror ``ProceduralSkyMaterial`` field for field,
which is what makes the cross-host parity fixture possible.
"""

from __future__ import annotations

from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, PointerProperty
from bpy.types import PropertyGroup, World

__all__ = ["ParadiseWorldProperties", "classes", "register_pointers", "unregister_pointers"]

AMBIENT_MODE_ITEMS = [
    ("Color", "Flat Color", "A single ambient colour in every direction"),
    (
        "Skybox",
        "Sky Gradient",
        "Integrate ambient from the procedural sky gradient below, including spherical harmonics",
    ),
]


class ParadiseWorldProperties(PropertyGroup):
    """Contract environment settings authored per world."""

    ambient_mode: EnumProperty(  # type: ignore[valid-type]
        name="Ambient", items=AMBIENT_MODE_ITEMS, default="Color"
    )

    ambient_energy: FloatProperty(  # type: ignore[valid-type]
        name="Ambient Energy",
        description="Multiplier on the ambient term",
        default=1.0,
        min=0.0,
    )

    sky_reflections: BoolProperty(  # type: ignore[valid-type]
        name="Sky Reflections",
        description="Let the sky contribute ambient specular, not just diffuse",
        default=True,
    )

    # -- Procedural sky gradient (mirrors Godot's ProceduralSkyMaterial) -----------------
    # Authored as sRGB, like Godot's colour pickers, and linearized at export.

    sky_top_color: FloatVectorProperty(  # type: ignore[valid-type]
        name="Sky Top", subtype="COLOR", size=3, default=(0.385, 0.454, 0.55), min=0.0, max=1.0
    )
    sky_horizon_color: FloatVectorProperty(  # type: ignore[valid-type]
        name="Sky Horizon", subtype="COLOR", size=3, default=(0.646, 0.656, 0.67), min=0.0, max=1.0
    )
    ground_bottom_color: FloatVectorProperty(  # type: ignore[valid-type]
        name="Ground Bottom", subtype="COLOR", size=3, default=(0.2, 0.169, 0.133), min=0.0, max=1.0
    )
    ground_horizon_color: FloatVectorProperty(  # type: ignore[valid-type]
        name="Ground Horizon", subtype="COLOR", size=3, default=(0.646, 0.656, 0.67), min=0.0, max=1.0
    )
    sky_curve: FloatProperty(  # type: ignore[valid-type]
        name="Sky Curve",
        description="Gradient falloff toward the horizon. The contract stores 0.6/curve",
        default=0.15,
        min=0.0001,
    )
    ground_curve: FloatProperty(  # type: ignore[valid-type]
        name="Ground Curve", default=0.02, min=0.0001
    )
    sky_energy: FloatProperty(name="Sky Energy", default=1.0, min=0.0)  # type: ignore[valid-type]
    ground_energy: FloatProperty(name="Ground Energy", default=1.0, min=0.0)  # type: ignore[valid-type]
    energy_multiplier: FloatProperty(  # type: ignore[valid-type]
        name="Energy Multiplier",
        description="Final linear scale on the whole sky colour",
        default=1.0,
        min=0.0,
    )
    sun_angle_max: FloatProperty(  # type: ignore[valid-type]
        name="Sun Halo Angle",
        description="Outer angle of the sun's halo, in degrees",
        default=30.0,
        min=0.0,
        max=180.0,
    )
    sun_curve: FloatProperty(name="Sun Curve", default=0.15, min=0.0001)  # type: ignore[valid-type]

    # -- Post-processing (EEVEE's AO/bloom moved between 4.1 and 4.2; not read) ------------

    ssao_enabled: BoolProperty(name="SSAO", default=False)  # type: ignore[valid-type]
    ssao_radius: FloatProperty(name="Radius", default=1.0, min=0.0)  # type: ignore[valid-type]
    ssao_intensity: FloatProperty(name="Intensity", default=2.0, min=0.0)  # type: ignore[valid-type]
    ssao_power: FloatProperty(name="Power", default=1.5, min=0.0)  # type: ignore[valid-type]

    glow_enabled: BoolProperty(name="Glow", default=False)  # type: ignore[valid-type]
    glow_intensity: FloatProperty(name="Intensity", default=0.6, min=0.0)  # type: ignore[valid-type]
    glow_threshold: FloatProperty(  # type: ignore[valid-type]
        name="HDR Threshold",
        description="Luminance above which pixels bloom",
        default=1.0,
        min=0.0,
    )

    fog_enabled: BoolProperty(name="Fog", default=False)  # type: ignore[valid-type]
    fog_color: FloatVectorProperty(  # type: ignore[valid-type]
        name="Fog Color", subtype="COLOR", size=3, default=(0.5, 0.52, 0.56), min=0.0, max=1.0
    )
    fog_density: FloatProperty(name="Density", default=0.01, min=0.0)  # type: ignore[valid-type]


classes = (ParadiseWorldProperties,)


def register_pointers() -> None:
    World.paradise = PointerProperty(type=ParadiseWorldProperties)


def unregister_pointers() -> None:
    if hasattr(World, "paradise"):
        del World.paradise
