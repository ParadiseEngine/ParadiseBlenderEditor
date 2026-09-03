"""Blender lights -> ``SceneLightData``.

Point/spot watts have no physical conversion to Godot's unitless energy, so the ratio maps
Blender's default lamp to Godot's default light. A sun IS exact: Godot folds pi into light energy
(Lambert divides by pi, energy is premultiplied), so ``E = S / pi`` makes a diffuse surface match.
Area lights export as points with ``AreaSize`` rather than vanishing into a dark scene.
"""

from __future__ import annotations

import math

import bpy

from .. import log
from ..contract import axes
from ..contract.color import Color32
from ..contract.schema import SceneLightData

__all__ = ["WATTS_PER_INTENSITY_UNIT", "export_light", "host_light_values", "light_type_name"]

#: Blender's default lamp is 100 W, Godot's default energy 1.0: a calibration, not physics.
WATTS_PER_INTENSITY_UNIT = 100.0


def light_type_name(light: bpy.types.Light) -> str:
    if light.type == "SUN":
        return "Directional"
    if light.type == "SPOT":
        return "Spot"
    return "Point"


def export_light(obj: bpy.types.Object) -> SceneLightData:
    light = obj.data
    matrix = obj.matrix_world

    # Lamps emit along local -Z (matching Godot); this is the direction light TRAVELS.
    basis_z = (matrix[0][2], matrix[1][2], matrix[2][2])
    forward_blender = (-basis_z[0], -basis_z[1], -basis_z[2])

    data = SceneLightData(
        id=obj.name,
        type=light_type_name(light),
        position=axes.convert_point(tuple(matrix.translation)),
        direction=axes.convert_direction(forward_blender),
        # Already linear, like material colours: no transfer function.
        color=Color32.from_rgba(light.color[0], light.color[1], light.color[2], 1.0),
        # Hidden or render-excluded is off; the contract has one flag.
        enabled=obj.visible_get() and not obj.hide_render,
        intensity=_intensity(light),
        shadows_enabled=light.use_shadow,
        specular=light.specular_factor,
        size=_size(light),
        # Blender has no per-light shadow opacity; full strength is the honest default.
        shadow_strength=1.0,
    )

    if light.type in {"POINT", "SPOT", "AREA"}:
        data.range = _range(light)
        # Blender is true inverse-square (exponent 2); Godot's default is 1.0.
        data.attenuation_exponent = 2.0

    if light.type == "SPOT":
        # spot_size is already the FULL cone; Godot's exporter doubles a half-angle. No doubling.
        data.spot_angle = math.degrees(light.spot_size)
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
    """A lamp as the HostLight field set, sharing the math with :func:`export_light`.
    ``SpotAngle`` stays in radians and ``Color`` is a linear Vector4, as ``HostLight`` declares."""
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
        # Godot folds pi into energy (module docstring); without this every sun is pi times too bright.
        return light.energy / math.pi
    return light.energy / WATTS_PER_INTENSITY_UNIT


def _size(light: bpy.types.Light) -> float:
    """Angular diameter in DEGREES for a sun, radius in metres otherwise (Godot's LIGHT_PARAM_SIZE)."""
    if light.type == "SUN":
        return math.degrees(light.angle)
    return light.shadow_soft_size


def _range(light: bpy.types.Light) -> float:
    """Influence radius, or 0 for unbounded."""
    if getattr(light, "use_custom_distance", False):
        return light.cutoff_distance
    return 0.0
