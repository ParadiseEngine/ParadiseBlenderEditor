"""Collider scale folding -- port of ``Paradise.Export.Geometry.ColliderScaleFold``.

The contract stores a collider shape in the **entity root's local space with the collider's
lossy scale folded into the shape dimensions**. The root's own scale is *not* folded: it stays
in the entity's ``WorldMatrix``, and the consumer re-applies it with these same rules (see
``Paradise.Sample.Runtime.SceneAssembler.AppendCollider``). Fold twice and every collider in
the scene comes out the wrong size.

Only the Y-aligned capsule is modeled. Godot's ``CapsuleShape3D`` is always Y-aligned, and
Blender has no capsule primitive at all -- ``authoring/collider.py`` derives one from an empty
or the object's bounds, and we adopt the same Y-aligned convention so both authoring hosts
produce identical data. A capsule along another axis is authored by rotating the collider
object; that rotation is captured separately in ``ColliderShapeData.LocalRotation``.
"""

from __future__ import annotations

from .axes import Vec3

__all__ = ["box_size", "capsule_height", "capsule_radius", "relative_scale", "sphere_radius"]

# Matches ColliderScaleFold.Divide: below this the divisor is treated as degenerate.
_EPSILON = 1e-6


def relative_scale(source_lossy_scale: Vec3, root_lossy_scale: Vec3) -> Vec3:
    """Per-component scale of a collider relative to its entity root.

    A zero-scaled root yields 0 rather than an infinity -- a flattened root is degenerate
    either way, and a NaN would propagate silently into the exported JSON.
    """
    return (
        _divide(source_lossy_scale[0], root_lossy_scale[0]),
        _divide(source_lossy_scale[1], root_lossy_scale[1]),
        _divide(source_lossy_scale[2], root_lossy_scale[2]),
    )


def box_size(size: Vec3, rel_scale: Vec3) -> Vec3:
    """Box full-size folded component-wise with ``abs(relative scale)``."""
    return (
        size[0] * abs(rel_scale[0]),
        size[1] * abs(rel_scale[1]),
        size[2] * abs(rel_scale[2]),
    )


def sphere_radius(radius: float, rel_scale: Vec3) -> float:
    """Sphere radius folded with the largest absolute scale axis.

    Taking the max (rather than an average) keeps the collider enclosing under non-uniform
    scale -- a sphere cannot become an ellipsoid in the contract.
    """
    return radius * max(abs(rel_scale[0]), abs(rel_scale[1]), abs(rel_scale[2]))


def capsule_radius(radius: float, rel_scale: Vec3) -> float:
    """Y-aligned capsule radius: folded with ``max(|x|, |z|)`` (the cross-section axes)."""
    return radius * max(abs(rel_scale[0]), abs(rel_scale[2]))


def capsule_height(height: float, rel_scale: Vec3) -> float:
    """Y-aligned capsule height: folded with ``|y|`` (the axis of the capsule)."""
    return height * abs(rel_scale[1])


def _divide(value: float, divisor: float) -> float:
    return 0.0 if abs(divisor) <= _EPSILON else value / divisor
