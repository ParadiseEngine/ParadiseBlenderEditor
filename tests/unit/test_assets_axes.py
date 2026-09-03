"""Tests for the document Y-up <-> Blender Z-up basis change, both directions.

A regression here does not crash anything: it rotates every object in every scene the addon
opens by 90 degrees, and saves that back over the source of truth.

Named ``test_assets_axes`` rather than ``test_axes`` because the sibling addon already has one
and pytest imports test modules by basename -- two files called ``test_axes.py`` in the same run
collide, and the second is silently skipped.
"""

from __future__ import annotations

import math

from paradise_assets.document import axes


def approx(actual, expected, tol=1e-9):
    assert len(actual) == len(expected)
    for a, e in zip(actual, expected, strict=True):
        assert abs(a - e) <= tol, f"{actual} != {expected}"


class TestBasis:
    def test_document_up_becomes_blender_up(self):
        # The document is Y-up; Blender is Z-up.
        m = axes.to_blender(axes.trs_to_matrix((0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)))
        approx((m[0][3], m[1][3], m[2][3]), (0.0, 0.0, 1.0))

    def test_document_forward_maps_to_blender_plus_y(self):
        # C is a rotation of -90 degrees about X, so document -Z lands on Blender +Y -- NOT on
        # Blender's -Y "forward". The two hosts' forward directions are not the same ray, and
        # paradise_blender's own pinned test agrees from the other side (Blender -Y -> document
        # +Z). Its CONVENTIONS.md table says "-Y (forward) | -Z", which contradicts both the
        # code and the test beside it; the code is right, being pinned against Blender's own
        # glTF exporter.
        m = axes.to_blender(axes.trs_to_matrix((0.0, 0.0, -1.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)))
        approx((m[0][3], m[1][3], m[2][3]), (0.0, 1.0, 0.0))

    def test_the_two_directions_are_inverses(self):
        m = axes.trs_to_matrix((1.0, 2.0, 3.0), (0.1, 0.2, 0.3, 0.927), (1.0, 2.0, 3.0))
        back = axes.to_document(axes.to_blender(m))
        for row_a, row_b in zip(m, back, strict=True):
            approx(row_a, row_b)

    def test_conjugation_distributes_over_composition(self):
        # The property that makes a parent/child hierarchy survive the rebase. Left-multiplying
        # instead of conjugating would fail exactly here and nowhere else.
        half = math.sqrt(0.5)
        a = axes.trs_to_matrix((1.0, 0.0, 0.0), (0.0, half, 0.0, half), (1.0, 1.0, 1.0))
        b = axes.trs_to_matrix((half, 2.0, 0.0), (half, 0.0, 0.0, half), (2.0, 2.0, 2.0))
        for row_a, row_b in zip(
            axes.to_blender(axes.matmul(a, b)),
            axes.matmul(axes.to_blender(a), axes.to_blender(b)),
            strict=True,
        ):
            approx(row_a, row_b)


class TestScale:
    def test_scale_axes_are_permuted_not_copied(self):
        # THE trap. Converting position/rotation/scale separately gets this wrong, because the
        # basis change permutes the axes: document (1, 2, 3) is Blender (1, 3, 2).
        _, _, scale = axes.to_blender_trs((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 2.0, 3.0))
        approx(scale, (1.0, 3.0, 2.0))

    def test_a_non_uniform_scale_survives_a_round_trip(self):
        # A genuinely unit quaternion, because the round trip normalizes: comparing against a
        # 1.000025-long input would measure the normalization rather than the conversion.
        half = math.sqrt(0.5)
        trs = ((1.0, 2.0, 3.0), (0.0, half, 0.0, half), (0.5, 2.0, 4.0))
        position, rotation, scale = axes.from_blender_trs(*axes.to_blender_trs(*trs))
        approx(position, trs[0])
        approx(scale, trs[2])
        assert abs(1.0 - abs(sum(x * y for x, y in zip(rotation, trs[1], strict=True)))) < 1e-9


class TestQuaternions:
    def test_a_non_unit_quaternion_is_normalized_before_composing(self):
        # Documents store float32-quantized quaternions, so none is exactly unit. Leaving the
        # length error in leaks it into the matrix, and the decompose reads it back as SCALE --
        # which turned a stored 20.0 into 19.999998 on ShiningPie's skyline props.
        _, _, scale = axes.to_blender_trs(
            (0.0, 0.0, 0.0), (0.0, 0.7071067, 0.0, 0.7071068), (20.0, 1.0, 20.0)
        )
        approx(scale, (20.0, 20.0, 1.0), tol=1e-9)

    def test_a_half_turn_decomposes_precisely(self):
        # Shepperd's method exists for this case: the naive w-first formula divides by
        # sqrt(1 + trace), which is zero for a 180-degree rotation -- and an axis-aligned scene
        # is full of them.
        trs = ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (1.0, 1.0, 1.0))
        _, rotation, _ = axes.from_blender_trs(*axes.to_blender_trs(*trs))
        assert all(math.isfinite(v) for v in rotation)
        assert abs(1.0 - abs(sum(x * y for x, y in zip(rotation, trs[1], strict=True)))) < 1e-9


class TestIdentity:
    def test_identity_stays_identity(self):
        position, rotation, scale = axes.to_blender_trs(
            (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)
        )
        approx(position, (0.0, 0.0, 0.0))
        approx(scale, (1.0, 1.0, 1.0))
        assert abs(abs(rotation[3]) - 1.0) < 1e-9
