"""Transform conversion between Blender's types and the contract's.

One rule governs everything here: **convert the matrix first, decompose second.**

It is tempting to decompose a Blender matrix into position/rotation/scale and convert each
piece. That works for position, works for rotation only if the quaternion conversion is right,
and quietly fails for scale -- a Blender scale of ``(1, 2, 3)`` is a contract scale of
``(1, 3, 2)``, because the basis change permutes the axes. Converting the assembled matrix and
decomposing the result gets all three right by construction, and it is the same order the
glTF exporter uses.
"""

from __future__ import annotations

from mathutils import Matrix

from ..contract import axes
from ..contract.matrix import trs

__all__ = ["decompose_contract", "to_contract_matrix", "to_mathutils"]


def to_contract_matrix(matrix: Matrix) -> axes.Mat4:
    """Rebase a ``mathutils.Matrix`` into the contract basis."""
    return axes.convert_matrix(axes.from_mathutils(matrix))


def to_mathutils(m: axes.Mat4) -> Matrix:
    """Adapt a contract matrix back to ``mathutils`` so Blender's decomposition can run on it."""
    return Matrix(tuple(tuple(row) for row in m))


def decompose_contract(
    matrix: Matrix,
) -> tuple[axes.Vec3, axes.Quat, axes.Vec3, axes.Mat4]:
    """Convert a Blender matrix and return ``(position, rotation, scale, contract_matrix)``.

    The rotation is in the contract's ``(x, y, z, w)`` order -- Blender's ``Quaternion`` is
    ``(w, x, y, z)``, and mixing the two produces a rotation that looks plausible and is wrong.

    The returned matrix is rebuilt from the decomposed parts via
    :func:`..contract.matrix.trs` rather than being the converted matrix itself. That keeps
    ``LocalMatrix`` exactly consistent with ``LocalPosition``/``Rotation``/``Scale`` in the
    emitted document -- a consumer reading either path gets the same transform, even for a
    sheared matrix where decomposition is lossy.
    """
    converted = to_contract_matrix(matrix)
    translation, rotation, scale = to_mathutils(converted).decompose()

    position = (translation.x, translation.y, translation.z)
    quaternion = (rotation.x, rotation.y, rotation.z, rotation.w)
    scale_tuple = (scale.x, scale.y, scale.z)

    return position, quaternion, scale_tuple, trs(position, quaternion, scale_tuple)
