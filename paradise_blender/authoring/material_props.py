"""Extra material parameters the contract carries but no DCC tool models natively.

``LevelMaterialData`` has fields with no counterpart in either Godot's ``StandardMaterial3D``
or Blender's Principled BSDF: the procedural animated-material recipes (``lava``, ``jade``,
``nebula``, ...) and glTF-style transmission. The Godot addon authors them as *resource
metadata* -- values Godot stores and ignores, which the exporter picks up. This module is the
same idea in Blender, as a property group on the material.

The invariant worth stating: **nothing here changes how Blender renders the material.** These
are instructions for the Paradise runtime shader. A material with ``material_kind = "lava"``
looks like whatever its Principled BSDF says inside Blender and looks like flowing lava in the
engine. That divergence is intentional but surprising, so the N-panel labels the section
accordingly.
"""

from __future__ import annotations

from bpy.props import EnumProperty, FloatProperty, FloatVectorProperty, PointerProperty
from bpy.types import Material, PropertyGroup

__all__ = [
    "ParadiseMaterialProperties",
    "classes",
    "register_pointers",
    "unregister_pointers",
]

# Recipe names understood by the runtime shader. The contract's "no recipe" value is the empty
# string, but Blender rejects an empty enum identifier (it warns "current value '0' matches no
# enum" and the property becomes unreadable), so the sentinel NONE is used here and mapped back
# to "" by resolved_material_kind at export.
NONE_KIND = "NONE"

MATERIAL_KIND_ITEMS = [
    (NONE_KIND, "None (PBR)", "An ordinary physically-based material"),
    ("lava", "Lava", "Flowing molten rock; emissive"),
    ("marble", "Marble", "Veined stone"),
    ("jade", "Jade", "Translucent green stone"),
    ("ice", "Ice", "Refractive frozen surface"),
    ("gem", "Gem", "Faceted transparent crystal"),
    ("molten_metal", "Molten Metal", "Glowing liquid metal"),
    ("obsidian", "Obsidian", "Black volcanic glass"),
    ("amber", "Amber", "Warm translucent resin"),
    ("nebula", "Nebula", "Animated star field"),
]


class ParadiseMaterialProperties(PropertyGroup):
    """Contract-only material parameters. None of these affect Blender's own viewport."""

    transmission_factor: FloatProperty(  # type: ignore[valid-type]
        name="Transmission",
        description=(
            "glTF-style transmission (0..1) driving the runtime's stylized glass path. "
            "Blender's Principled BSDF transmission is not read: it is a physical refraction "
            "parameter, this is a stylized-shader input, and equating them looks wrong"
        ),
        default=0.0,
        min=0.0,
        max=1.0,
    )

    material_kind: EnumProperty(  # type: ignore[valid-type]
        name="Recipe",
        description="Procedural animated material recipe evaluated by the runtime shader",
        items=MATERIAL_KIND_ITEMS,
        default=NONE_KIND,
    )

    emissive_strength: FloatProperty(  # type: ignore[valid-type]
        name="Emissive Strength",
        description=(
            "UNCLAMPED HDR multiplier on the emissive factor -- values above 1 are the point "
            "(they let lava bloom past white)"
        ),
        default=1.0,
        min=0.0,
    )

    noise_scale: FloatProperty(name="Noise Scale", default=1.0, min=0.0)  # type: ignore[valid-type]
    flow_speed: FloatProperty(name="Flow Speed", default=1.0)  # type: ignore[valid-type]

    color_a: FloatVectorProperty(  # type: ignore[valid-type]
        name="Color A",
        description="Primary tint for tintable recipes. Authored as sRGB; linearized at export",
        subtype="COLOR",
        size=3,
        default=(1.0, 1.0, 1.0),
        min=0.0,
        max=1.0,
    )
    color_b: FloatVectorProperty(  # type: ignore[valid-type]
        name="Color B",
        description="Secondary tint for tintable recipes",
        subtype="COLOR",
        size=3,
        default=(0.0, 0.0, 0.0),
        min=0.0,
        max=1.0,
    )


def resolved_material_kind(props: ParadiseMaterialProperties) -> str:
    """The contract value: the empty string when no recipe is selected.

    Bridges the :data:`NONE_KIND` sentinel back to what ``LevelMaterialData.MaterialKind``
    expects, so the sentinel never escapes the authoring layer.
    """
    return "" if props.material_kind == NONE_KIND else props.material_kind


classes = (ParadiseMaterialProperties,)


def register_pointers() -> None:
    Material.paradise = PointerProperty(type=ParadiseMaterialProperties)


def unregister_pointers() -> None:
    if hasattr(Material, "paradise"):
        del Material.paradise
