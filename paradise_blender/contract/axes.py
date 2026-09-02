"""Blender (Z-up, -Y forward) <-> contract (Y-up, -Z forward) basis change: Rot(-90°, X)::

    C @ (x, y, z) = (x, z, -y)          M_contract = C @ M_blender @ C^-1

Conjugation, never left-multiplication: ``C @ M`` rotates the placement but leaves the local
axes in the old basis, which breaks the moment transforms compose (any parent/child). Both
bases are right-handed, so C is a pure rotation and winding needs no flip. This is exactly what
Blender's glTF exporter does for ``export_yup=True``, pinned by ``test_axis_parity.py``; a
disagreement lands every mesh rotated 90° about X.

No ``bpy``/``mathutils`` import; row-major ``m[row][col]`` tuples, adapted at :func:`from_mathutils`.
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
    "convert_point_inverse",
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
    """The one boundary between Blender types and the pure math."""
    return tuple(tuple(float(v) for v in row) for row in matrix)  # type: ignore[return-value]


def convert_matrix(m: Mat4) -> Mat4:
    """Rebase a transform: ``C @ m @ C^-1``."""
    return matmul(matmul(_C, m), _C_INV)


def convert_point(v: Vec3) -> Vec3:
    """Rebase a position or any point-like vector: ``(x, y, z) -> (x, z, -y)``."""
    x, y, z = v
    return (x, z, -y)


# A direction rebases identically to a point: C is linear, and being a pure rotation it is
# its own normal matrix (inverse-transpose == itself), so normals need no special case.
convert_direction = convert_point


def convert_point_inverse(v: Vec3) -> Vec3:
    """Inverse of :func:`convert_point`: ``(x, y, z) -> (x, -z, y)``."""
    x, y, z = v
    return (x, -z, y)


def convert_quaternion(q: Quat) -> Quat:
    """Rebase an ``(x, y, z, w)`` quaternion (rotate the imaginary part, keep w). Blender's
    ``Quaternion`` is ``(w, x, y, z)``; callers must reorder first."""
    x, y, z, w = q
    ax, ay, az = convert_direction((x, y, z))
    return (ax, ay, az, w)


def convert_euler_zyx_degrees(euler_xyz_radians: Vec3) -> Vec3:
    """Rebase Blender XYZ Euler radians to contract Euler degrees via the matrix, since an
    Euler-to-Euler formula is a minefield of gimbal and order conventions. Unused since v5."""
    m = convert_matrix(_euler_xyz_to_matrix(euler_xyz_radians))
    return tuple(math.degrees(a) for a in _matrix_to_euler_xyz(m))  # type: ignore[return-value]


def convert_triangle_indices(indices: list[int]) -> list[int]:
    """Unchanged: C is a proper rotation and cannot mirror. Exists so the no-flip is explicit
    at call sites, where a left-handed pipeline WOULD need one."""
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
