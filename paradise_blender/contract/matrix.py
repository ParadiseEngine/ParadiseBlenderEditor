"""Matrix/vector/quaternion shapes as the contract serializes them.

Port of ``Paradise.Export.Geometry.ContractMatrix`` plus the vector converters in
``ContractConverters.cs``.

The layout rule is the fiddly part, and it is inherited from Unity via the original
``ParadiseUnityEditor``: ``LocalMatrix`` / ``WorldMatrix`` use the **column-vector**
convention -- basis vectors in the matrix *columns*, translation in the last *column* -- and
are then flattened **column-major**. The net effect a consumer sees is translation at flat
indices **12, 13, 14** (verifiable in any exported scene: a uniformly scaled entity at the
origin reads ``[s,0,0,0, 0,s,0,0, 0,0,s,0, 0,0,0,1]``).

That flat layout happens to equal a plain row-major flatten of the *transposed* matrix, which
is how :func:`flatten_column_major` is implemented. The distinction matters only if someone
later swaps in a row-vector TRS -- hence :func:`trs` builds and transposes explicitly rather
than hard-coding the final numbers.

Rotations arrive here already rebased into the contract basis (see :mod:`.axes`); nothing in
this module changes handedness.
"""

from __future__ import annotations

from .axes import Mat4, Quat, Vec3, identity, matmul

__all__ = [
    "flatten_column_major",
    "quat_to_json",
    "quaternion_to_matrix",
    "trs",
    "vec2_to_json",
    "vec3_to_json",
]


def quaternion_to_matrix(q: Quat) -> Mat4:
    """Rotation matrix for an ``(x, y, z, w)`` quaternion, row-major."""
    x, y, z, w = q
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return (
        (1.0 - 2.0 * (yy + zz), 2.0 * (xy - wz), 2.0 * (xz + wy), 0.0),
        (2.0 * (xy + wz), 1.0 - 2.0 * (xx + zz), 2.0 * (yz - wx), 0.0),
        (2.0 * (xz - wy), 2.0 * (yz + wx), 1.0 - 2.0 * (xx + yy), 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )


def trs(translation: Vec3, rotation: Quat, scale: Vec3) -> Mat4:
    """Compose a transform in the contract's column-vector layout.

    Scale is applied first, then rotation, then translation -- the same order as
    ``ContractMatrix.Trs``. The result is returned in column-vector form (translation in the
    last column), ready for :func:`flatten_column_major`.
    """
    scale_m: Mat4 = (
        (scale[0], 0.0, 0.0, 0.0),
        (0.0, scale[1], 0.0, 0.0),
        (0.0, 0.0, scale[2], 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    rotate_m = quaternion_to_matrix(rotation)
    result = matmul(rotate_m, scale_m)
    return (
        (result[0][0], result[0][1], result[0][2], translation[0]),
        (result[1][0], result[1][1], result[1][2], translation[1]),
        (result[2][0], result[2][1], result[2][2], translation[2]),
        (0.0, 0.0, 0.0, 1.0),
    )


def flatten_column_major(m: Mat4) -> list[float]:
    """Flatten a row-major ``m[row][col]`` matrix column-major, as the contract stores it.

    For a column-vector transform this puts translation at flat indices 12/13/14.
    """
    return [m[row][col] for col in range(4) for row in range(4)]


def vec3_to_json(v: Vec3) -> list[float]:
    return [float(v[0]), float(v[1]), float(v[2])]


def vec2_to_json(v) -> list[float]:  # any 2-sequence
    return [float(v[0]), float(v[1])]


def quat_to_json(q: Quat) -> list[float]:
    """Serialize as ``[x, y, z, w]`` -- System.Numerics order, not Blender's ``(w, x, y, z)``."""
    return [float(q[0]), float(q[1]), float(q[2]), float(q[3])]


IDENTITY_MATRIX = identity()
