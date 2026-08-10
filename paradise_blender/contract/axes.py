"""Blender (Z-up) <-> Paradise contract (Y-up) basis change.

This is the one place the Blender addon diverges structurally from the Godot addon.
``ParadiseGodotEditor`` writes transforms verbatim because Godot's basis IS the contract's
basis -- right-handed, Y-up, -Z forward. Blender is right-handed but **Z-up, -Y forward**,
so every transform, vector, and triangle that leaves Blender must be rebased.

The conversion is a rotation of -90 degrees about X::

    C = | 1  0  0 |          C @ (x, y, z) = (x, z, -y)
        | 0  0  1 |
        | 0 -1  0 |

Both bases are right-handed, so ``C`` is a pure rotation (det = +1): winding order and
chirality are preserved and no mirroring correction is needed anywhere.

Transforms are rebased by **conjugation**, not by left-multiplication::

    M_contract = C @ M_blender @ C^-1

Left-multiplying alone (``C @ M``) rotates an object's placement but leaves its local axes
expressed in the old basis, which breaks the moment a transform is composed with another --
i.e. any parent/child hierarchy. Conjugation is a similarity transform: it re-expresses the
*same* linear map in the new basis, so composition survives it
(``C(AB)C^-1 == (CAC^-1)(CBC^-1)``).

Crucially this is also exactly what Blender's own glTF exporter does for ``export_yup=True``.
That matters because ``RenderableComponentData.Mesh`` points at a GLB written by that
exporter: if our node transforms disagreed with its vertex data, every mesh in the scene
would land rotated 90 degrees about X. ``tests/integration/test_axis_parity.py`` pins this
against real glTF exporter output.

This module is deliberately free of any ``bpy`` / ``mathutils`` import so it can be unit
tested with plain ``pytest``. Matrices are plain nested tuples in row-major
``m[row][col]`` order (the same indexing ``mathutils.Matrix`` uses), and
:func:`from_mathutils` adapts Blender's type at the boundary.
"""

from __future__ import annotations

import math

__all__ = [
    "Mat4",
    "Quat",
    "Vec3",
    "convert_direction",
    "convert_euler_zyx_degrees",
    "convert_matrix",
    "convert_point",
    "convert_quaternion",
    "convert_triangle_indices",
    "from_mathutils",
    "identity",
    "matmul",
]

# Row-major 4x4: m[row][col]. Matches mathutils.Matrix indexing.
Mat4 = tuple[
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
]
Vec3 = tuple[float, float, float]
Quat = tuple[float, float, float, float]  # (x, y, z, w) -- contract order, NOT Blender's wxyz

# C: blender -> contract. Rotation(-90 deg, X).
_C: Mat4 = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, -1.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)

# C^-1 == C transposed (C is a rotation). Rotation(+90 deg, X).
_C_INV: Mat4 = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, -1.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)


def identity() -> Mat4:
    """The 4x4 identity, in this module's row-major tuple layout."""
    return (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )


def matmul(a: Mat4, b: Mat4) -> Mat4:
    """Standard row-major 4x4 product ``a @ b``."""
    return tuple(  # type: ignore[return-value]
        tuple(sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)) for r in range(4)
    )


def from_mathutils(matrix) -> Mat4:  # mathutils.Matrix, not importable here
    """Adapt a ``mathutils.Matrix`` to this module's plain-tuple layout.

    Kept as the single boundary between Blender types and the pure contract math, so every
    other function here stays unit-testable without Blender.
    """
    return tuple(tuple(float(v) for v in row) for row in matrix)  # type: ignore[return-value]


def convert_matrix(m: Mat4) -> Mat4:
    """Rebase a Blender transform into the contract basis: ``C @ m @ C^-1``.

    Use for anything that is a *transform* -- object world/local matrices, bone matrices,
    collider poses. For a bare position use :func:`convert_point`, which is the cheaper
    equivalent of taking the translation column of the result.
    """
    return matmul(matmul(_C, m), _C_INV)


def convert_point(v: Vec3) -> Vec3:
    """Rebase a position or any point-like vector: ``(x, y, z) -> (x, z, -y)``."""
    x, y, z = v
    return (x, z, -y)


# A direction rebases identically to a point: C is linear, and being a pure rotation it is
# its own normal matrix (inverse-transpose == itself), so normals need no special case.
convert_direction = convert_point


def convert_quaternion(q: Quat) -> Quat:
    """Rebase a rotation given as an ``(x, y, z, w)`` quaternion.

    A similarity transform by a rotation ``C`` acts on quaternions by rotating the imaginary
    part with ``C`` and leaving ``w`` alone -- the axis moves to the new basis, the angle is
    unchanged. That is the quaternion form of ``C @ R @ C^-1``.

    Note the component order: Blender's ``Quaternion`` is ``(w, x, y, z)`` while the
    contract (System.Numerics) is ``(x, y, z, w)``. Callers must reorder before calling.
    """
    x, y, z, w = q
    ax, ay, az = convert_direction((x, y, z))
    return (ax, ay, az, w)


def convert_euler_zyx_degrees(euler_xyz_radians: Vec3) -> Vec3:
    """Rebase Blender XYZ Euler angles (radians) to contract Euler degrees.

    Only ``CameraData.Rotation`` consumes Euler angles; everything else in the contract uses
    matrices or quaternions. Rather than deriving an Euler-to-Euler formula (which is a
    minefield of gimbal and order conventions), this builds the rotation matrix, rebases it,
    and decomposes back -- slower, but obviously correct.
    """
    m = convert_matrix(_euler_xyz_to_matrix(euler_xyz_radians))
    return tuple(math.degrees(a) for a in _matrix_to_euler_xyz(m))  # type: ignore[return-value]


def convert_triangle_indices(indices: list[int]) -> list[int]:
    """Triangle winding is preserved by the conversion -- returned unchanged.

    ``C`` is a proper rotation (det = +1), so it does not mirror: a counter-clockwise
    triangle stays counter-clockwise and face normals keep pointing the same way relative to
    their geometry. This function exists to make that reasoning explicit at every call site
    (navmesh, collision geometry) rather than leaving a silent no-op, because the equivalent
    step in a left-handed pipeline *would* need a flip.
    """
    return list(indices)


def _euler_xyz_to_matrix(euler: Vec3) -> Mat4:
    """Blender's XYZ Euler convention: R = Rz @ Ry @ Rx (X applied first)."""
    rx, ry, rz = euler
    sx, cx = math.sin(rx), math.cos(rx)
    sy, cy = math.sin(ry), math.cos(ry)
    sz, cz = math.sin(rz), math.cos(rz)
    return (
        (cy * cz, cz * sx * sy - cx * sz, cx * cz * sy + sx * sz, 0.0),
        (cy * sz, cx * cz + sx * sy * sz, -cz * sx + cx * sy * sz, 0.0),
        (-sy, cy * sx, cx * cy, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )


def _matrix_to_euler_xyz(m: Mat4) -> Vec3:
    """Inverse of :func:`_euler_xyz_to_matrix`, with the gimbal-lock branch handled."""
    sy = -m[2][0]
    if sy >= 1.0 - 1e-6:  # +90 deg pitch: X and Z degenerate, fold rotation into X.
        return (math.atan2(m[0][1], m[1][1]), math.pi / 2.0, 0.0)
    if sy <= -1.0 + 1e-6:
        return (math.atan2(-m[0][1], m[1][1]), -math.pi / 2.0, 0.0)
    return (
        math.atan2(m[2][1], m[2][2]),
        math.asin(max(-1.0, min(1.0, sy))),
        math.atan2(m[1][0], m[0][0]),
    )
