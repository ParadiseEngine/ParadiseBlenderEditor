"""Blender (Z-up, -Y forward) <-> document (Y-up, -Z forward), both directions: Rot(-90°, X)::

    C @ (x, y, z) = (x, z, -y)     M_document = C @ M_blender @ C^-1     M_blender = C^-1 @ M @ C

Conjugation, never left-multiplication, or local axes stay in the old basis and composition
(any parent/child) breaks. Convert the MATRIX and decompose second: the basis change permutes
axes, so document scale (1, 2, 3) is Blender scale (1, 3, 2), and converting channels separately
silently gets that wrong. Duplicates ``paradise_blender/contract/axes.py`` because extensions
cannot import each other (#35). No ``bpy``/``mathutils``; row-major ``m[row][col]`` tuples.
"""

from __future__ import annotations

import math

__all__ = [
    "Mat4",
    "Quat",
    "Vec3",
    "from_blender_trs",
    "identity",
    "matmul",
    "to_blender",
    "to_blender_trs",
    "to_document",
    "trs_to_matrix",
]

Mat4 = tuple[
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
]
Vec3 = tuple[float, float, float]
Quat = tuple[float, float, float, float]  # (x, y, z, w) -- document order, NOT Blender's wxyz

# C: blender -> document. Rotation(-90 deg, X).
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


def to_document(m: Mat4) -> Mat4:
    """Rebase a Blender transform into the document basis: ``C @ m @ C^-1``."""
    return matmul(matmul(_C, m), _C_INV)


def to_blender(m: Mat4) -> Mat4:
    """Rebase a document transform into Blender's basis: ``C^-1 @ m @ C``."""
    return matmul(matmul(_C_INV, m), _C)


def trs_to_matrix(position: Vec3, rotation: Quat, scale: Vec3) -> Mat4:
    """Compose a TRS. The quaternion is NORMALIZED first: document quaternions are
    float32-quantized and never exactly unit, and the length error comes back out of the
    decompose as SCALE (a stored ``20.0`` became ``19.999998`` on ShiningPie's skyline props)."""
    x, y, z, w = rotation
    length = math.sqrt(x * x + y * y + z * z + w * w)
    if length > 0.0:
        x, y, z, w = x / length, y / length, z / length, w / length

    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    r = (
        (1.0 - 2.0 * (yy + zz), 2.0 * (xy - wz), 2.0 * (xz + wy)),
        (2.0 * (xy + wz), 1.0 - 2.0 * (xx + zz), 2.0 * (yz - wx)),
        (2.0 * (xz - wy), 2.0 * (yz + wx), 1.0 - 2.0 * (xx + yy)),
    )
    sx, sy, sz = scale
    s = (sx, sy, sz)
    return (
        (r[0][0] * s[0], r[0][1] * s[1], r[0][2] * s[2], position[0]),
        (r[1][0] * s[0], r[1][1] * s[1], r[1][2] * s[2], position[1]),
        (r[2][0] * s[0], r[2][1] * s[1], r[2][2] * s[2], position[2]),
        (0.0, 0.0, 0.0, 1.0),
    )


def to_blender_trs(position: Vec3, rotation: Quat, scale: Vec3) -> tuple[Vec3, Quat, Vec3]:
    """Document TRS -> Blender TRS (compose, rebase, decompose); rotation stays ``(x, y, z, w)``."""
    return _decompose(to_blender(trs_to_matrix(position, rotation, scale)))


def from_blender_trs(position: Vec3, rotation: Quat, scale: Vec3) -> tuple[Vec3, Quat, Vec3]:
    """A Blender TRS as a document TRS -- the inverse of :func:`to_blender_trs`."""
    return _decompose(to_document(trs_to_matrix(position, rotation, scale)))


def _decompose(m: Mat4) -> tuple[Vec3, Quat, Vec3]:
    """Split into TRS; a mirrored basis (det < 0) keeps its negative scale on X rather than
    silently becoming a different shape."""
    translation = (m[0][3], m[1][3], m[2][3])

    columns = [(m[0][c], m[1][c], m[2][c]) for c in range(3)]
    scale = [math.sqrt(sum(v * v for v in col)) for col in columns]
    if _determinant3(m) < 0.0:
        scale[0] = -scale[0]

    basis = []
    for col, length in zip(columns, scale, strict=True):
        basis.append((0.0, 0.0, 0.0) if length == 0.0 else tuple(v / length for v in col))

    return translation, _matrix_to_quaternion(basis), (scale[0], scale[1], scale[2])


def _determinant3(m: Mat4) -> float:
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


def _matrix_to_quaternion(basis) -> Quat:
    """Shepperd's method: the naive w-first formula loses all precision at 180°, which
    axis-aligned props hit constantly."""
    m00, m01, m02 = basis[0][0], basis[1][0], basis[2][0]
    m10, m11, m12 = basis[0][1], basis[1][1], basis[2][1]
    m20, m21, m22 = basis[0][2], basis[1][2], basis[2][2]

    trace = m00 + m11 + m22
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        return ((m21 - m12) / s, (m02 - m20) / s, (m10 - m01) / s, 0.25 * s)
    if m00 > m11 and m00 > m22:
        s = math.sqrt(1.0 + m00 - m11 - m22) * 2.0
        return (0.25 * s, (m01 + m10) / s, (m02 + m20) / s, (m21 - m12) / s)
    if m11 > m22:
        s = math.sqrt(1.0 + m11 - m00 - m22) * 2.0
        return ((m01 + m10) / s, 0.25 * s, (m12 + m21) / s, (m02 - m20) / s)
    s = math.sqrt(1.0 + m22 - m00 - m11) * 2.0
    return ((m02 + m20) / s, (m12 + m21) / s, 0.25 * s, (m10 - m01) / s)
