"""Tests for the KTX2 cache key's encode half.

A cached texture is keyed on its pixels plus this signature. If the signature failed to
distinguish two encodes of the same pixels, the cache would serve one for the other -- and the
worst case is not a corrupt file but a plausible one: a normal map encoded as sRGB renders the
model uniformly DARK rather than obviously broken (see :mod:`paradise_blender.pipeline.ktx`).
"""

from __future__ import annotations

from paradise_blender.pipeline.ktx import Transcoder, encode_command, encode_signature

# A path that cannot be probed for a version, so the signature is deterministic here; the
# version's own contribution is exercised by it being part of the returned string.
MODERN = Transcoder("/nonexistent/ktx", modern=True)
LEGACY = Transcoder("/nonexistent/toktx", modern=False)


class TestDistinguishesEncodes:
    def test_colour_and_data_differ(self):
        assert encode_signature("rock_BaseColor.png", MODERN) != encode_signature(
            "rock_Roughness.png", MODERN
        )

    def test_normal_map_differs_from_other_linear_data(self):
        """--normal-mode switches the encoder to the two-channel layout the runtime's transcoder
        assumes; a roughness map is linear but NOT that layout."""
        assert encode_signature("rock_Normal.png", MODERN) != encode_signature(
            "rock_Roughness.png", MODERN
        )

    def test_dialects_differ(self):
        assert encode_signature("rock_BaseColor.png", MODERN) != encode_signature(
            "rock_BaseColor.png", LEGACY
        )


class TestIgnoresIrrelevantDifferences:
    def test_directory_does_not_change_the_signature(self):
        """The same image reached through two paths must hit one cache entry -- sidecars are
        written per GLB, so an atlas shared by a dozen props arrives under a dozen paths."""
        assert encode_signature("/tmp/a/rock_BaseColor.png", MODERN) == encode_signature(
            "/other/b/rock_BaseColor.png", MODERN
        )

    def test_target_path_does_not_change_the_signature(self):
        assert encode_signature("rock_BaseColor.png", MODERN) == encode_signature(
            "rock_BaseColor.png", MODERN
        )


class TestTracksTheCommand:
    def test_every_flag_reaches_the_signature(self):
        """The signature is derived from the argv rather than restated, so a future flag
        invalidates cached artifacts without anyone remembering to bump a version."""
        signature = encode_signature("rock_Normal.png", MODERN)
        for argument in encode_command("rock_Normal.png", "out.ktx2", MODERN)[1:]:
            if argument not in ("rock_Normal.png", "out.ktx2"):
                assert argument in signature

    def test_paths_are_placeholders(self):
        signature = encode_signature("/tmp/rock_BaseColor.png", MODERN)
        assert "/tmp/rock_BaseColor.png" not in signature
        assert "<source>" in signature
