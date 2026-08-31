"""Tests for sRGB conversion and the contract's 8-bit Color32 encoding."""

from __future__ import annotations

import pytest

from paradise_blender.contract.color import (
    Color32,
    linear_to_srgb,
    srgb_to_linear,
    to_byte,
)


class TestSrgbToLinear:
    def test_endpoints_are_fixed(self):
        assert srgb_to_linear(0.0) == pytest.approx(0.0)
        assert srgb_to_linear(1.0) == pytest.approx(1.0)

    def test_mid_grey_darkens(self):
        """The classic check: sRGB 0.5 is linear ~0.214, not 0.5. Skipping this conversion
        is what makes an exported scene look washed out against the Godot render."""
        assert srgb_to_linear(0.5) == pytest.approx(0.2140, abs=1e-4)

    def test_linear_segment_near_black(self):
        # Below the 0.04045 knee the transfer function is linear (divide by 12.92).
        assert srgb_to_linear(0.02) == pytest.approx(0.02 / 12.92)

    def test_round_trips_with_inverse(self):
        for value in (0.0, 0.01, 0.04, 0.2, 0.5, 0.9, 1.0):
            assert linear_to_srgb(srgb_to_linear(value)) == pytest.approx(value, abs=1e-9)


class TestToByte:
    @pytest.mark.parametrize(
        ("value", "expected"), [(0.0, 0), (1.0, 255), (0.5, 128), (2.0, 255), (-1.0, 0)]
    )
    def test_quantization_and_clamping(self, value, expected):
        assert to_byte(value) == expected

    def test_rounds_half_away_from_zero_not_bankers(self):
        """Python's built-in ``round`` is round-half-to-even and would return 0 here; C#'s
        ``MidpointRounding.AwayFromZero`` returns 1. A one-byte difference on every affected
        channel would fail the conformance gate."""
        assert to_byte(0.5 / 255) == 1

    def test_non_finite_inputs(self):
        assert to_byte(float("nan")) == 0
        assert to_byte(float("-inf")) == 0
        assert to_byte(float("inf")) == 255


class TestColor32:
    def test_json_is_the_packed_bytes_as_hex(self):
        color = Color32.from_rgba(8 / 255, 0.0, 0.0, 1.0)
        assert color.to_json() == "#080000FF"

    def test_alpha_is_always_written(self):
        """Fixed nine characters, so a reader never has to guess whether a short form meant
        opaque or malformed."""
        assert Color32.from_rgba(1, 1, 1).to_json() == "#FFFFFFFF"
        assert len(Color32.from_rgba(0, 0, 0, 0).to_json()) == 9

    def test_from_srgb_linearizes_rgb_but_not_alpha(self):
        """Alpha is coverage, not light -- applying a transfer function to it would make
        every semi-transparent material the wrong opacity."""
        color = Color32.from_srgb(0.5, 0.5, 0.5, 0.5)
        expected_rgb = to_byte(srgb_to_linear(0.5))
        assert color.to_json() == f"#{expected_rgb:02X}{expected_rgb:02X}{expected_rgb:02X}80"

    def test_precision_loss_is_the_contract(self):
        """Two distinct floats that quantize to the same byte are equal in the contract."""
        assert Color32.from_rgba(0.5001, 0, 0) == Color32.from_rgba(0.5002, 0, 0)

    def test_is_hashable_for_dedup(self):
        assert len({Color32.from_rgba(1, 1, 1), Color32.from_rgba(1, 1, 1)}) == 1
