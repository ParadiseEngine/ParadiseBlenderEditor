"""Ambient irradiance from a sky, in the contract's Y-up space.

Port of the sky math in ``SceneDataExporter.cs`` (``EvalProceduralSky``,
``IntegrateSkyIrradiance``, ``ProjectSkyIrradianceSh``), generalized so the *radiance source*
is a callable rather than being hard-wired to Godot's ``ProceduralSkyMaterial``.

That generalization is the point. The engine's ``EnvironmentData`` describes a two-part sky
gradient plus a sun disk because that is Godot's procedural sky -- but the numbers the runtime
actually consumes for diffuse ambient are the *integrated* ones (``AmbientColor``,
``AmbientEquatorColor``, ``AmbientGroundColor``, ``AmbientSh``). Any host that can evaluate its
own sky per direction can fill those in. Blender's world can be a flat color, a Background
node, or a Sky Texture; ``export/world.py`` picks an evaluator and this module turns it into
the same contract fields Godot produces.

**All directions and outputs here are in contract space (Y-up).** Callers convert Blender's
Z-up vectors first -- see :mod:`.axes`.

Two conventions worth stating because they are easy to get wrong by a factor of pi:

* The integral returns ``E/pi`` -- the cosine-weighted *average radiance* over the hemisphere,
  which is exactly the ambient value the engine expects (measured against Godot: sky-SH ambient
  equals ``albedo * E/pi``). Multiplying by pi here makes every scene pi times too bright.
* The SH band factors are ``A_l/pi = (1, 2/3, 1/4)``, premultiplied into the coefficients, so
  reconstruction yields the same ``E/pi``. The shader applies only the basis constants.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from .axes import Vec3

__all__ = [
    "Rgb",
    "SkyRadiance",
    "SunParams",
    "flat_sky",
    "godot_procedural_sky",
    "integrate_irradiance",
    "project_irradiance_sh",
]

Rgb = tuple[float, float, float]

#: Evaluates sky radiance (linear RGB) for a unit direction in contract space.
SkyRadiance = Callable[[Vec3], Rgb]

# Sample counts match the C# implementation so both hosts land on the same Monte-Carlo answer.
_IRRADIANCE_SAMPLES = 1024
_SH_SAMPLES = 4096
_GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))


class SunParams:
    """Sun disk/halo parameters, matching Godot's ``sky_material.cpp`` uniforms.

    ``size`` and ``angle_max`` are **cosines** of angles, so the disk test is a plain dot-product
    comparison. The contract's "no sun" sentinel is 2.0 -- out of range for a cosine, so the
    branch can never trigger.
    """

    __slots__ = ("angle_max", "color_energy", "direction", "enabled", "inv_curve", "size")

    def __init__(
        self,
        enabled: bool = False,
        direction: Vec3 = (0.0, 1.0, 0.0),
        color_energy: Rgb = (0.0, 0.0, 0.0),
        size: float = 2.0,
        angle_max: float = 2.0,
        inv_curve: float = 24.0,
    ) -> None:
        self.enabled = enabled
        self.direction = direction
        self.color_energy = color_energy
        self.size = size
        self.angle_max = angle_max
        self.inv_curve = inv_curve


def flat_sky(color: Rgb) -> SkyRadiance:
    """A uniform sky. Integrating it returns the color itself, which is the correct ambient
    for Blender's default "flat background color" world."""

    def evaluate(_direction: Vec3) -> Rgb:
        return color

    return evaluate


def godot_procedural_sky(
    sky_top: Rgb,
    sky_horizon: Rgb,
    ground_bottom: Rgb,
    ground_horizon: Rgb,
    inv_sky_curve: float,
    inv_ground_curve: float,
    sun: SunParams | None = None,
) -> SkyRadiance:
    """Godot's ``ProceduralSkyMaterial`` radiance as a reusable evaluator.

    Retained even though Blender has no such material: a Blender scene authored to match a
    Godot scene can select this evaluator and get byte-identical ambient, which is how the
    cross-host parity fixture is built. All colors are linear and already premultiplied by
    their energy multipliers, as in the C# original.
    """
    sun = sun or SunParams()

    def evaluate(direction: Vec3) -> Rgb:
        v = min(max(direction[1], -1.0), 1.0)
        if direction[1] >= 0.0:
            t = min(max(math.pow(1.0 - v, inv_sky_curve), 0.0), 1.0)
            color = _lerp(sky_top, sky_horizon, t)
        else:
            t = min(max(math.pow(1.0 + v, inv_ground_curve), 0.0), 1.0)
            color = _lerp(ground_bottom, ground_horizon, t)

        if sun.enabled:
            sun_angle = _dot(sun.direction, direction)
            if sun_angle > sun.size:
                color = sun.color_energy
            elif sun_angle > sun.angle_max:
                c2 = (sun.size - sun_angle) / (sun.size - sun.angle_max)
                blend = min(max(math.pow(1.0 - c2, sun.inv_curve), 0.0), 1.0)
                color = _lerp(color, sun.color_energy, blend)
        return color

    return evaluate


def integrate_irradiance(normal: Vec3, radiance: SkyRadiance, energy: float = 1.0) -> Rgb:
    """Cosine-weighted average radiance over the hemisphere around ``normal`` (i.e. ``E/pi``).

    Fibonacci-sphere sampling over the full sphere, rejecting the back hemisphere. Used for
    the three ambient zones the contract stores: up (sky), horizontal (equator), down (ground).
    """
    r = g = b = weight_sum = 0.0
    for i in range(_IRRADIANCE_SAMPLES):
        direction = _fibonacci_direction(i, _IRRADIANCE_SAMPLES)
        n_dot_l = _dot(normal, direction)
        if n_dot_l <= 0.0:
            continue
        cr, cg, cb = radiance(direction)
        r += cr * n_dot_l
        g += cg * n_dot_l
        b += cb * n_dot_l
        weight_sum += n_dot_l

    if weight_sum <= 0.0:
        return (0.0, 0.0, 0.0)
    return (r / weight_sum * energy, g / weight_sum * energy, b / weight_sum * energy)


def project_irradiance_sh(radiance: SkyRadiance, energy: float = 1.0) -> list[float]:
    """Project sky radiance onto L2 spherical harmonics: 9 RGB coefficients = 27 floats.

    Ramamoorthi ordering (Y00, Y1-1, Y10, Y11, Y2-2, Y2-1, Y20, Y21, Y22), with the band
    factors and energy multiplier premultiplied. Coefficients may be negative -- that is
    normal for SH, and why the contract stores full-precision floats here instead of the
    8-bit ``Color32`` it uses for the zone colors.
    """
    coefficients = [0.0] * 27
    for i in range(_SH_SAMPLES):
        d = _fibonacci_direction(i, _SH_SAMPLES)
        cr, cg, cb = radiance(d)
        basis = _sh_basis(d)
        for k in range(9):
            coefficients[k * 3 + 0] += cr * basis[k]
            coefficients[k * 3 + 1] += cg * basis[k]
            coefficients[k * 3 + 2] += cb * basis[k]

    # Monte-Carlo weight (4*pi/N) times the cosine-lobe band factors divided by pi.
    weight = 4.0 * math.pi / _SH_SAMPLES * energy
    band_hat = (1.0, 2.0 / 3.0, 2.0 / 3.0, 2.0 / 3.0, 0.25, 0.25, 0.25, 0.25, 0.25)
    for k in range(9):
        scale = weight * band_hat[k]
        coefficients[k * 3 + 0] *= scale
        coefficients[k * 3 + 1] *= scale
        coefficients[k * 3 + 2] *= scale
    return coefficients


def _sh_basis(d: Vec3) -> tuple[float, ...]:
    x, y, z = d
    return (
        0.282095,
        0.488603 * y,
        0.488603 * z,
        0.488603 * x,
        1.092548 * x * y,
        1.092548 * y * z,
        0.315392 * (3.0 * z * z - 1.0),
        1.092548 * x * z,
        0.546274 * (x * x - y * y),
    )


def _fibonacci_direction(index: int, count: int) -> Vec3:
    """Evenly distributed direction on the unit sphere, Y as the polar axis (contract up)."""
    y = 1.0 - (index + 0.5) / count * 2.0
    radius = math.sqrt(max(0.0, 1.0 - y * y))
    theta = _GOLDEN_ANGLE * index
    return (math.cos(theta) * radius, y, math.sin(theta) * radius)


def _lerp(a: Rgb, b: Rgb, t: float) -> Rgb:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
