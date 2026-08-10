"""Tests for collider scale folding and the collision-layer contract."""

from __future__ import annotations

import pytest

from paradise_blender.contract import collider_fold, layers


class TestRelativeScale:
    def test_uniform_root_and_child(self):
        assert collider_fold.relative_scale((2.0, 2.0, 2.0), (2.0, 2.0, 2.0)) == (1.0, 1.0, 1.0)

    def test_child_scaled_relative_to_root(self):
        assert collider_fold.relative_scale((4.0, 2.0, 6.0), (2.0, 2.0, 2.0)) == (2.0, 1.0, 3.0)

    def test_degenerate_root_yields_zero_not_infinity(self):
        """A flattened root is degenerate either way, but a NaN or inf would propagate
        silently into the exported JSON and only surface as a broken collider at runtime."""
        assert collider_fold.relative_scale((1.0, 1.0, 1.0), (0.0, 1.0, 1.0)) == (0.0, 1.0, 1.0)


class TestShapeFolding:
    def test_box_folds_per_axis(self):
        assert collider_fold.box_size((1.0, 2.0, 3.0), (2.0, 3.0, 4.0)) == (2.0, 6.0, 12.0)

    def test_box_uses_absolute_scale(self):
        """A negative (mirrored) scale must not produce a negative extent."""
        assert collider_fold.box_size((1.0, 1.0, 1.0), (-2.0, 1.0, 1.0)) == (2.0, 1.0, 1.0)

    def test_sphere_takes_the_largest_axis(self):
        """A sphere cannot become an ellipsoid in the contract, so the fold must stay
        enclosing under non-uniform scale."""
        assert collider_fold.sphere_radius(1.0, (2.0, 5.0, 3.0)) == 5.0

    def test_capsule_radius_uses_cross_section_axes_only(self):
        # Y is the capsule's axis, so it must not influence the radius.
        assert collider_fold.capsule_radius(1.0, (2.0, 99.0, 3.0)) == 3.0

    def test_capsule_height_uses_the_axis(self):
        assert collider_fold.capsule_height(2.0, (9.0, 3.0, 9.0)) == 6.0

    def test_capsule_height_uses_absolute_scale(self):
        assert collider_fold.capsule_height(2.0, (1.0, -3.0, 1.0)) == 6.0


class TestCollisionLayers:
    @pytest.mark.parametrize(
        ("mask", "index"), [(0, 0), (1, 0), (2, 1), (4, 2), (8, 3), (1 << 19, 19)]
    )
    def test_mask_to_index_takes_the_lowest_bit(self, mask, index):
        assert layers.mask_to_layer_index(mask) == index

    def test_unlayered_body_maps_to_zero(self):
        assert layers.mask_to_layer_index(0) == 0

    def test_multi_layer_is_detected(self):
        """The single-int contract cannot carry multi-layer membership. Detecting it is what
        lets the exporter warn instead of silently dropping the author's other layers."""
        assert layers.is_multi_layer(0b0101)
        assert not layers.is_multi_layer(0b0100)
        assert not layers.is_multi_layer(0)

    def test_multi_layer_keeps_the_lowest(self):
        assert layers.mask_to_layer_index(0b1010) == 1

    def test_round_trip_through_the_consumer_side_inverse(self):
        """The runtime rebuilds the mask as ``1u << Layer``; for single-layer bodies that must
        return the original mask."""
        for index in range(20):
            mask = layers.layer_index_to_mask(index)
            assert layers.mask_to_layer_index(mask) == index
