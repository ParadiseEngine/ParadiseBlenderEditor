"""Transform conversion. Convert the MATRIX first, decompose second: converting channels
separately quietly fails for scale, since (1, 2, 3) in Blender is (1, 3, 2) in the contract.
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
    """``(position, (x, y, z, w) rotation, scale, matrix)``; Blender's ``Quaternion`` is
    ``(w, x, y, z)`` and mixing them looks plausible and is wrong. The matrix is rebuilt from the
    decomposed parts so both paths agree even for a sheared matrix."""
    converted = to_contract_matrix(matrix)
    translation, rotation, scale = to_mathutils(converted).decompose()

    position = (translation.x, translation.y, translation.z)
    quaternion = (rotation.x, rotation.y, rotation.z, rotation.w)
    scale_tuple = (scale.x, scale.y, scale.z)

    return position, quaternion, scale_tuple, trs(position, quaternion, scale_tuple)
