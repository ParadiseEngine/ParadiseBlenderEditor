"""Blender lights -> ``SceneLightData``.

Two conversions here are approximations rather than mappings, and both are documented at
their constants because they change how bright an exported scene looks:

* **Intensity units.** Blender measures point/spot/area light output in *watts* of radiant
  power; the contract carries a unitless multiplier in Godot's convention. There is no
  physically correct conversion without also fixing an exposure model, so the ratio is chosen
  so that Blender's default light maps to Godot's default light -- defaults look the same in
  both hosts, and an author scaling from there scales predictably. Sun lamps DO have an exact
  conversion: strength is irradiance in W/m^2, and Godot folds a factor of pi into light
  energy (its Lambert divides by pi, then energy is premultiplied by pi), so a diffuse
  surface under a contract-energy-E sun renders ``albedo * E * NdotL`` while Blender renders
  ``albedo * S/pi * NdotL``. Exporting ``E = S / pi`` makes the two hosts match exactly.

* **Area lights.** Neither the contract nor Godot has an area light type. Rather than dropping
  them (a scene would go dark with no explanation) they export as point lights with their
  dimensions recorded in ``AreaSize``, and the exporter says so.

Blender lights, like Godot's, aim along their local -Z, so the direction convention carries
over unchanged apart from the basis rebase.
"""

from __future__ import annotations

import math

import bpy

from .. import log
from ..contract import axes
from ..contract.color import Color32
from ..contract.schema import SceneLightData

__all__ = ["WATTS_PER_INTENSITY_UNIT", "export_light", "host_light_values", "light_type_name"]

#: Blender's default point/spot lamp is 100 W; the contract's (and Godot's) default light
#: energy is 1.0. Dividing by this maps default to default, so a scene lit "normally" in
#: Blender arrives lit normally in the engine. It is a calibration constant, not physics.
WATTS_PER_INTENSITY_UNIT = 100.0


def light_type_name(light: bpy.types.Light) -> str:
    if light.type == "SUN":
        return "Directional"
    if light.type == "SPOT":
        return "Spot"
    # POINT and AREA both land on Point; AREA is warned about by the caller.
    return "Point"


def export_light(obj: bpy.types.Object) -> SceneLightData:
    light = obj.data
    matrix = obj.matrix_world

    # Blender lamps emit along local -Z, matching Godot. Take the third basis column, negate,
    # then rebase into contract space.
    basis_z = (matrix[0][2], matrix[1][2], matrix[2][2])
    forward_blender = (-basis_z[0], -basis_z[1], -basis_z[2])

    data = SceneLightData(
        id=obj.name,
        type=light_type_name(light),
        position=axes.convert_point(tuple(matrix.translation)),
        direction=axes.convert_direction(forward_blender),
        # Blender light colours are linear scene-referred, like its material colours, so no
        # sRGB transfer function here (unlike the Godot host, which authors in sRGB).
        color=Color32.from_rgba(light.color[0], light.color[1], light.color[2], 1.0),
        # A light hidden in the viewport or excluded from renders is off, as far as an author
        # is concerned; the contract has one flag for both.
        enabled=obj.visible_get() and not obj.hide_render,
        intensity=_intensity(light),
        shadows_enabled=light.use_shadow,
        specular=light.specular_factor,
        size=_size(light),
        # The contract's shadow strength is Godot's shadow_opacity (1 = fully dark). Blender
        # has no per-light equivalent, so full strength is the honest default.
        shadow_strength=1.0,
    )

    if light.type in {"POINT", "SPOT", "AREA"}:
        data.range = _range(light)
        # Godot's default attenuation exponent is 1.0 (inverse-linear, not inverse-square).
        # Blender uses true inverse-square falloff, which is exponent 2.
        data.attenuation_exponent = 2.0

    if light.type == "SPOT":
        # Blender's spot_size is already the FULL cone angle -- unlike Godot's SpotAngle,
        # which is the half-angle and gets doubled by that exporter. No doubling here.
        data.spot_angle = math.degrees(light.spot_size)
        # spot_blend is a 0..1 fraction of the cone that is soft edge.
        data.inner_spot_angle = math.degrees(light.spot_size * (1.0 - light.spot_blend))

    if light.type == "AREA":
        size_y = light.size_y if light.shape in {"RECTANGLE", "ELLIPSE"} else light.size
        data.area_size = (light.size, size_y)
        log.warn(
            f"Light '{obj.name}' is an area light. Neither the contract nor the engine has an "
            "area light type, so it is exported as a point light with its dimensions recorded "
            "in AreaSize. Expect a harder falloff than Blender shows."
        )

    return data


def host_light_values(obj: bpy.types.Object) -> dict | None:
    """A lamp as the HostLight field set, in the storage shape ``build_payload`` consumes.

    Shares intensity, aim and falloff with :func:`export_light` so a light cannot describe
    itself one way as a scene lamp and another as an authored host reference. Two differences
    are load-bearing: ``SpotAngle`` stays in radians (that is what ``HostLight`` declares), and
    ``Color`` is the lamp's linear Vector4 rather than a quantized ``Color32``.
    """
    if obj.type != "LIGHT" or obj.data is None:
        return None
    data = export_light(obj)
    color = obj.data.color
    return {
        "Type": data.type,
        "Position": list(data.position),
        "Direction": list(data.direction),
        "Color": [float(color[0]), float(color[1]), float(color[2]), 1.0],
        "Enabled": data.enabled,
        "Intensity": data.intensity,
        "ShadowsEnabled": data.shadows_enabled,
        "ShadowStrength": data.shadow_strength,
        "Specular": data.specular,
        "Size": data.size,
        "Range": data.range,
        "SpotAngle": float(obj.data.spot_size) if obj.data.type == "SPOT" else 0.0,
        "AttenuationExponent": data.attenuation_exponent,
    }


def _intensity(light: bpy.types.Light) -> float:
    """Contract intensity multiplier. See :data:`WATTS_PER_INTENSITY_UNIT`."""
    if light.type == "SUN":
        # Sun strength is irradiance in W/m^2; the contract multiplier is Godot energy, which
        # carries a folded-in factor of pi (see the module docstring). Dividing by pi makes a
        # diffuse surface come out the same brightness in Blender and in the engine.
        return light.energy / math.pi
    return light.energy / WATTS_PER_INTENSITY_UNIT


def _size(light: bpy.types.Light) -> float:
    """Light size: angular diameter in DEGREES for a sun, world radius in meters otherwise.

    The two meanings share one contract field (mirroring Godot's LIGHT_PARAM_SIZE), which is
    why the unit depends on the type.
    """
    if light.type == "SUN":
        return math.degrees(light.angle)
    return light.shadow_soft_size


def _range(light: bpy.types.Light) -> float:
    """Influence radius, or 0 for unbounded.

    Blender only bounds a light when custom distance is enabled; otherwise its influence is
    unlimited and 0 tells the runtime the same thing.
    """
    if getattr(light, "use_custom_distance", False):
        return light.cutoff_distance
    return 0.0
