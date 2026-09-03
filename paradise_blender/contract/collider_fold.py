"""Collider scale folding (port of ``ColliderScaleFold``). The collider's scale relative to the
entity root is folded into the shape; the root's own scale is NOT, since the consumer re-applies
it, and folding twice makes every collider the wrong size. Only the Y-aligned capsule exists
(Godot's ``CapsuleShape3D``); other axes are authored by rotating the object.
"""

from __future__ import annotations

from .axes import Vec3

__all__ = ["box_size", "capsule_height", "capsule_radius", "relative_scale", "sphere_radius"]

# Matches ColliderScaleFold.Divide: below this the divisor is treated as degenerate.
_EPSILON = 1e-6


def relative_scale(source_lossy_scale: Vec3, root_lossy_scale: Vec3) -> Vec3:
    """Scale relative to the root; a zero-scaled root yields 0, not a NaN that reaches the JSON."""
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
    """Sphere radius folded with the largest axis, so it stays enclosing under non-uniform scale."""
    return radius * max(abs(rel_scale[0]), abs(rel_scale[1]), abs(rel_scale[2]))


def capsule_radius(radius: float, rel_scale: Vec3) -> float:
    """Y-aligned capsule radius: folded with ``max(|x|, |z|)`` (the cross-section axes)."""
    return radius * max(abs(rel_scale[0]), abs(rel_scale[2]))


def capsule_height(height: float, rel_scale: Vec3) -> float:
    """Y-aligned capsule height: folded with ``|y|`` (the axis of the capsule)."""
    return height * abs(rel_scale[1])


def _divide(value: float, divisor: float) -> float:
    return 0.0 if abs(divisor) <= _EPSILON else value / divisor
