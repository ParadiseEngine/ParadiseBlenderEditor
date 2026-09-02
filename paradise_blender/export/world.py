"""Blender world + view settings -> ``EnvironmentData``. Tone mapping and background come from
Blender; the sky gradient, AO and bloom from authored properties (Blender has no equivalent, or
one that moved between versions). Ambient is integrated by :mod:`..contract.sky`, the same code
path the Godot host uses.
"""

from __future__ import annotations

import math

import bpy

from ..contract import sky as sky_math
from ..contract.color import Color32, linear_to_srgb, srgb_to_linear
from ..contract.schema import EnvironmentData

__all__ = ["export_environment", "find_sun", "resolve_background_color"]

# View transform -> Godot ToneMapper name. Filmic/AgX are not the same curves as Godot's, but
# the closest named operator beats forcing Linear.
_VIEW_TRANSFORM_TO_TONEMAP = {
    "Standard": "Linear",
    "Raw": "Linear",
    "Filmic": "Filmic",
    "Filmic Log": "Filmic",
    "AgX": "Agx",
    "ACES": "Aces",
    "Khronos PBR Neutral": "Filmic",
}


def find_sun(scene: bpy.types.Scene) -> bpy.types.Object | None:
    """The first visible sun lamp, matching the Godot host's ``FindSun``."""
    for obj in scene.objects:
        if obj.type == "LIGHT" and obj.data.type == "SUN" and obj.visible_get():
            return obj
    return None


def resolve_background_color(world: bpy.types.World | None) -> Color32:
    """The world's flat clear colour, in the contract's linear encoding."""
    rgb = _background_rgb(world)
    return Color32.from_rgba(*rgb, 1.0)


def export_environment(scene: bpy.types.Scene) -> EnvironmentData:
    """Build the contract environment for a scene."""
    data = EnvironmentData()
    world = scene.world

    _apply_view_transform(scene, data)

    if world is None:
        # No world: non-authoritative background so the runtime keeps its own clear.
        data.has_background = False
        return data

    props = world.paradise
    data.has_background = True
    data.ambient_energy = props.ambient_energy
    data.sky_reflections = props.sky_reflections

    _apply_post_processing(props, data)

    if props.ambient_mode == "Skybox":
        _apply_sky_gradient(scene, props, data)
    else:
        _apply_flat_ambient(world, data)

    return data


def _apply_view_transform(scene: bpy.types.Scene, data: EnvironmentData) -> None:
    view = scene.view_settings
    data.tonemap_mode = _VIEW_TRANSFORM_TO_TONEMAP.get(view.view_transform, "Linear")
    # Blender's exposure is in stops; the contract's is a linear multiplier.
    data.tonemap_exposure = math.pow(2.0, view.exposure)
    data.exposure = data.tonemap_exposure
    data.tonemap_white = 1.0


def _apply_post_processing(props, data: EnvironmentData) -> None:
    data.ssao_enabled = props.ssao_enabled
    data.ssao_radius = props.ssao_radius
    data.ssao_intensity = props.ssao_intensity
    data.ssao_power = props.ssao_power

    data.glow_enabled = props.glow_enabled
    data.glow_intensity = props.glow_intensity
    data.glow_threshold = props.glow_threshold

    data.fog_enabled = props.fog_enabled
    data.fog_color = Color32.from_srgb(*props.fog_color, 1.0)
    data.fog_density = props.fog_density


def _apply_flat_ambient(world: bpy.types.World, data: EnvironmentData) -> None:
    """Uniform ambient from the background; ``AmbientSh`` stays null per the contract."""
    data.ambient_mode = "Color"
    rgb = _background_rgb(world)
    ambient = Color32.from_rgba(*rgb, 1.0)
    data.ambient_color = ambient
    data.ambient_equator_color = ambient
    data.ambient_ground_color = ambient
    data.ambient_sh = None
    data.background_color = ambient
    data.sky_gradient = False


def _apply_sky_gradient(scene: bpy.types.Scene, props, data: EnvironmentData) -> None:
    """Ambient from the authored sky gradient. Energies apply where Godot applies them: sky and
    ground in sRGB (the setters), the overall multiplier in linear on the integral."""
    data.ambient_mode = "Skybox"
    data.sky_gradient = True

    sky_energy = props.sky_energy
    ground_energy = props.ground_energy
    energy = props.energy_multiplier

    sky_top = _srgb_scaled(props.sky_top_color, sky_energy)
    sky_horizon = _srgb_scaled(props.sky_horizon_color, sky_energy)
    ground_bottom = _srgb_scaled(props.ground_bottom_color, ground_energy)
    ground_horizon = _srgb_scaled(props.ground_horizon_color, ground_energy)

    # Godot: inv_sky_curve = 0.6 / sky_curve, inv_ground_curve = 0.6 / ground_curve.
    inv_sky_curve = 0.6 / props.sky_curve if props.sky_curve > 1e-4 else 4.0
    inv_ground_curve = 0.6 / props.ground_curve if props.ground_curve > 1e-4 else 30.0

    sun_params = _sun_params(scene, props)
    radiance = sky_math.godot_procedural_sky(
        sky_top, sky_horizon, ground_bottom, ground_horizon, inv_sky_curve, inv_ground_curve,
        sun=sun_params,
    )

    data.ambient_color = _to_color32(sky_math.integrate_irradiance((0.0, 1.0, 0.0), radiance, energy))
    data.ambient_equator_color = _to_color32(
        sky_math.integrate_irradiance((0.0, 0.0, 1.0), radiance, energy)
    )
    data.ambient_ground_color = _to_color32(
        sky_math.integrate_irradiance((0.0, -1.0, 0.0), radiance, energy)
    )
    data.ambient_sh = sky_math.project_irradiance_sh(radiance, energy)

    # Endpoints stored sRGB-encoded and UNtonemapped: tonemap(lerp) != lerp(tonemap), so
    # tone-mapped endpoints would hue-shift the mid-gradient.
    data.sky_top_color = _to_srgb_color32(sky_top, energy)
    data.sky_horizon_color = _to_srgb_color32(sky_horizon, energy)
    data.sky_ground_bottom_color = _to_srgb_color32(ground_bottom, energy)
    data.sky_ground_horizon_color = _to_srgb_color32(ground_horizon, energy)
    data.sky_sky_curve_inv = inv_sky_curve
    data.sky_ground_curve_inv = inv_ground_curve

    data.sky_sun_size_cos = sun_params.size
    data.sky_sun_angle_max_cos = sun_params.angle_max
    data.sky_sun_inv_curve = sun_params.inv_curve

    # Kept sRGB: the clear bypasses the shader's tonemap/OETF.
    data.background_color = Color32.from_rgba(*props.ground_bottom_color, 1.0)


def _sun_params(scene: bpy.types.Scene, props) -> sky_math.SunParams:
    """Sun disk parameters from the scene's sun lamp, in contract space."""
    from ..contract import axes

    sun = find_sun(scene)
    inv_sun_curve = 1.6 / math.pow(props.sun_curve, 1.4) if props.sun_curve > 1e-4 else 24.0

    if sun is None:
        # 2.0 is out of cosine range: the disk branch can never trigger.
        return sky_math.SunParams(enabled=False, inv_curve=inv_sun_curve)

    matrix = sun.matrix_world
    # Direction TO the sun: the lamp shines along -Z, so the sun sits along +Z.
    basis_z = (matrix[0][2], matrix[1][2], matrix[2][2])
    to_sun = axes.convert_direction(_normalize(basis_z))

    light = sun.data
    energy = light.energy
    color = (light.color[0] * energy, light.color[1] * energy, light.color[2] * energy)

    return sky_math.SunParams(
        enabled=True,
        direction=to_sun,
        color_energy=color,
        # light.angle is the sun's angular DIAMETER in radians.
        size=math.cos(light.angle),
        angle_max=math.cos(math.radians(props.sun_angle_max)),
        inv_curve=inv_sun_curve,
    )


def _background_rgb(world: bpy.types.World | None) -> tuple[float, float, float]:
    """Linear RGB of the world background (already linear; no transfer function)."""
    if world is None:
        return (0.05, 0.05, 0.05)

    if world.node_tree is None or not getattr(world, "use_nodes", True):
        return tuple(world.color)

    background = _find_background_node(world)
    if background is None:
        # A node tree cannot be evaluated from Python without rendering.
        return tuple(world.color)

    color_input = background.inputs.get("Color")
    strength_input = background.inputs.get("Strength")
    strength = strength_input.default_value if strength_input is not None else 1.0

    if color_input is None or color_input.is_linked:
        return tuple(c * strength for c in world.color)

    r, g, b = color_input.default_value[:3]
    return (r * strength, g * strength, b * strength)


def _find_background_node(world: bpy.types.World) -> bpy.types.Node | None:
    output = next(
        (n for n in world.node_tree.nodes if n.type == "OUTPUT_WORLD" and n.is_active_output), None
    )
    if output is None:
        return None
    surface = output.inputs.get("Surface")
    if surface is None or not surface.is_linked:
        return None
    node = surface.links[0].from_node
    return node if node.type == "BACKGROUND" else None


def _srgb_scaled(srgb_rgb, energy: float) -> tuple[float, float, float]:
    """Multiply in sRGB (where Godot's ``set_sky_*_color`` setters do), then linearize."""
    return tuple(srgb_to_linear(min(max(c * energy, 0.0), 1.0)) for c in srgb_rgb)


def _to_color32(linear_rgb) -> Color32:
    return Color32.from_rgba(linear_rgb[0], linear_rgb[1], linear_rgb[2], 1.0)


def _to_srgb_color32(linear_rgb, energy: float) -> Color32:
    """Apply the linear energy multiplier, then re-encode to sRGB for storage."""
    scaled = tuple(min(max(c * energy, 0.0), 1.0) for c in linear_rgb)
    return Color32.from_rgba(*(linear_to_srgb(c) for c in scaled), 1.0)


def _normalize(v) -> tuple[float, float, float]:
    length = math.sqrt(sum(c * c for c in v))
    return tuple(c / length for c in v) if length > 1e-9 else (0.0, 0.0, 1.0)
