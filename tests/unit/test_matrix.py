"""Tests for the contract's matrix layout.

The layout is inherited from Unity via ParadiseUnityEditor and is easy to get subtly wrong:
column-vector convention, flattened column-major, translation at flat indices 12/13/14. A
transposed result still looks like a plausible matrix, so these assertions are explicit about
where each number lands.
"""

from __future__ import annotations

import math

import pytest

from paradise_blender.contract import matrix


class TestFlattenColumnMajor:
    def test_translation_lands_at_indices_12_13_14(self):
        """The defining property of the contract's layout.

        Verified against a real export: an entity at the origin scaled 20x1x20 reads
        ``[20,0,0,0, 0,1,0,0, 0,0,20,0, 0,0,0,1]``.
        """
        m = matrix.trs((2.0, 3.0, 5.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0))
        flat = matrix.flatten_column_major(m)
        assert flat[12:15] == [2.0, 3.0, 5.0]
        assert flat[15] == 1.0

    def test_matches_exported_scale_matrix(self):
        m = matrix.trs((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (20.0, 1.0, 20.0))
        assert matrix.flatten_column_major(m) == [
            20, 0, 0, 0,
            0, 1, 0, 0,
            0, 0, 20, 0,
            0, 0, 0, 1,
        ]

    def test_is_a_column_major_read(self):
        m = (
            (0.0, 1.0, 2.0, 3.0),
            (4.0, 5.0, 6.0, 7.0),
            (8.0, 9.0, 10.0, 11.0),
            (12.0, 13.0, 14.0, 15.0),
        )
        assert matrix.flatten_column_major(m) == [
            0, 4, 8, 12,
            1, 5, 9, 13,
            2, 6, 10, 14,
            3, 7, 11, 15,
        ]


class TestTrs:
    def test_identity(self):
        m = matrix.trs((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0))
        assert matrix.flatten_column_major(m) == [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]

    def test_scale_is_applied_before_rotation(self):
        """Order matters: rotate-then-scale would shear a non-uniformly scaled object."""
        quarter_turn_z = (0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4))
        m = matrix.trs((0.0, 0.0, 0.0), quarter_turn_z, (2.0, 3.0, 1.0))
        # The rotated X basis vector keeps X's scale (2), now pointing along +Y.
        assert m[0][0] == pytest.approx(0.0, abs=1e-9)
        assert m[1][0] == pytest.approx(2.0)
        assert m[0][1] == pytest.approx(-3.0)

    def test_translation_is_unaffected_by_scale(self):
        m = matrix.trs((1.0, 2.0, 3.0), (0.0, 0.0, 0.0, 1.0), (10.0, 10.0, 10.0))
        assert (m[0][3], m[1][3], m[2][3]) == (1.0, 2.0, 3.0)


class TestQuaternionToMatrix:
    def test_identity(self):
        m = matrix.quaternion_to_matrix((0.0, 0.0, 0.0, 1.0))
        for row in range(3):
            for col in range(3):
                assert m[row][col] == pytest.approx(1.0 if row == col else 0.0)

    def test_90_degrees_about_y(self):
        q = (0.0, math.sin(math.pi / 4), 0.0, math.cos(math.pi / 4))
        m = matrix.quaternion_to_matrix(q)
        # +X rotates to -Z (right-handed, Y-up).
        assert (m[0][0], m[1][0], m[2][0]) == pytest.approx((0.0, 0.0, -1.0), abs=1e-9)

    def test_is_orthonormal(self):
        q = _normalize_quat((0.2, -0.5, 0.3, 0.9))
        m = matrix.quaternion_to_matrix(q)
        for col in range(3):
            length = math.sqrt(sum(m[row][col] ** 2 for row in range(3)))
            assert length == pytest.approx(1.0)


class TestJsonShapes:
    def test_quaternion_uses_xyzw_order(self):
        """Blender stores ``(w, x, y, z)``; the contract stores ``(x, y, z, w)``. Getting this
        backwards produces a valid-looking but wrong rotation, so it is asserted explicitly."""
        assert matrix.quat_to_json((1.0, 2.0, 3.0, 4.0)) == [1.0, 2.0, 3.0, 4.0]

    def test_vec3(self):
        assert matrix.vec3_to_json((1, 2, 3)) == [1.0, 2.0, 3.0]

    def test_vec2(self):
        assert matrix.vec2_to_json((1, 2)) == [1.0, 2.0]


def _normalize_quat(q):
    length = math.sqrt(sum(c * c for c in q))
    return tuple(c / length for c in q)
