"""Tests for sky irradiance integration and SH projection.

The factor-of-pi convention is the thing worth guarding here: the integral returns ``E/pi``
(cosine-weighted average radiance), which is what the engine consumes directly. An extra pi
makes every scene pi times too bright, and it is the kind of error that looks like an artistic
choice rather than a bug.
"""

from __future__ import annotations

import math

import pytest

from paradise_blender.contract import sky

UP = (0.0, 1.0, 0.0)
DOWN = (0.0, -1.0, 0.0)
SIDE = (0.0, 0.0, 1.0)


class TestFlatSky:
    def test_integrating_a_uniform_sky_returns_that_color(self):
        """The E/pi convention, stated as a test: for uniform radiance L, the cosine-weighted
        average is exactly L -- no pi anywhere."""
        result = sky.integrate_irradiance(UP, sky.flat_sky((0.25, 0.5, 0.75)))
        assert result == pytest.approx((0.25, 0.5, 0.75), abs=1e-6)

    def test_is_independent_of_normal_direction(self):
        radiance = sky.flat_sky((0.4, 0.4, 0.4))
        for normal in (UP, DOWN, SIDE):
            assert sky.integrate_irradiance(normal, radiance) == pytest.approx(
                (0.4, 0.4, 0.4), abs=1e-6
            )

    def test_energy_multiplier_scales_linearly(self):
        radiance = sky.flat_sky((0.2, 0.2, 0.2))
        base = sky.integrate_irradiance(UP, radiance)
        scaled = sky.integrate_irradiance(UP, radiance, energy=3.0)
        assert scaled == pytest.approx(tuple(c * 3.0 for c in base), abs=1e-6)

    def test_black_sky_contributes_nothing(self):
        assert sky.integrate_irradiance(UP, sky.flat_sky((0.0, 0.0, 0.0))) == (0.0, 0.0, 0.0)


class TestGradientSky:
    @staticmethod
    def _gradient():
        # Bright sky above, dark ground below, linear-ish curves.
        return sky.godot_procedural_sky(
            sky_top=(0.0, 0.0, 1.0),
            sky_horizon=(0.0, 0.0, 1.0),
            ground_bottom=(0.1, 0.0, 0.0),
            ground_horizon=(0.1, 0.0, 0.0),
            inv_sky_curve=1.0,
            inv_ground_curve=1.0,
        )

    def test_up_normal_sees_the_sky_and_down_normal_sees_the_ground(self):
        radiance = self._gradient()
        up = sky.integrate_irradiance(UP, radiance)
        down = sky.integrate_irradiance(DOWN, radiance)
        assert up[2] > 0.9 and up[0] < 0.05
        assert down[0] > 0.09 and down[2] < 0.05

    def test_horizontal_normal_sees_a_mix(self):
        result = sky.integrate_irradiance(SIDE, self._gradient())
        assert 0.0 < result[0] < 0.1
        assert 0.0 < result[2] < 1.0

    def test_evaluator_returns_top_color_straight_up(self):
        radiance = sky.godot_procedural_sky(
            sky_top=(1.0, 0.0, 0.0),
            sky_horizon=(0.0, 1.0, 0.0),
            ground_bottom=(0.0, 0.0, 1.0),
            ground_horizon=(1.0, 1.0, 0.0),
            inv_sky_curve=1.0,
            inv_ground_curve=1.0,
        )
        assert radiance(UP) == pytest.approx((1.0, 0.0, 0.0))
        assert radiance(DOWN) == pytest.approx((0.0, 0.0, 1.0))


class TestSunDisk:
    def test_disabled_sun_never_contributes(self):
        """The 2.0 sentinel is out of cosine range, so the disk branch is unreachable."""
        radiance = sky.godot_procedural_sky(
            (0.1, 0.1, 0.1), (0.1, 0.1, 0.1), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 1.0, 1.0,
            sun=sky.SunParams(enabled=False),
        )
        assert radiance(UP) == pytest.approx((0.1, 0.1, 0.1))

    def test_enabled_sun_saturates_within_the_disk(self):
        sun = sky.SunParams(
            enabled=True,
            direction=UP,
            color_energy=(5.0, 5.0, 5.0),
            size=math.cos(math.radians(1.0)),
            angle_max=math.cos(math.radians(10.0)),
            inv_curve=24.0,
        )
        radiance = sky.godot_procedural_sky(
            (0.1, 0.1, 0.1), (0.1, 0.1, 0.1), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 1.0, 1.0, sun=sun
        )
        assert radiance(UP) == pytest.approx((5.0, 5.0, 5.0))
        # Far from the sun, only the gradient remains.
        assert radiance(SIDE) == pytest.approx((0.1, 0.1, 0.1))

    def test_sun_brightens_the_ambient_it_is_integrated_into(self):
        """Godot's sky includes the sun disk in its ambient; leaving it out makes the
        gradient alone integrate visibly too dim."""
        base = sky.godot_procedural_sky(
            (0.1, 0.1, 0.1), (0.1, 0.1, 0.1), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 1.0, 1.0
        )
        with_sun = sky.godot_procedural_sky(
            (0.1, 0.1, 0.1), (0.1, 0.1, 0.1), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 1.0, 1.0,
            sun=sky.SunParams(
                enabled=True,
                direction=UP,
                color_energy=(10.0, 10.0, 10.0),
                size=math.cos(math.radians(5.0)),
                angle_max=math.cos(math.radians(20.0)),
            ),
        )
        assert sky.integrate_irradiance(UP, with_sun)[0] > sky.integrate_irradiance(UP, base)[0]


class TestSphericalHarmonics:
    def test_returns_27_floats(self):
        assert len(sky.project_irradiance_sh(sky.flat_sky((0.5, 0.5, 0.5)))) == 27

    def test_uniform_sky_has_only_a_dc_term(self):
        """A constant function projects entirely onto the L0 band; higher bands must vanish."""
        coefficients = sky.project_irradiance_sh(sky.flat_sky((0.5, 0.5, 0.5)))
        # For constant radiance L the DC coefficient is L * Y00 * 4*pi (the band factor for
        # L0 is 1). Reconstruction then multiplies by Y00 again, giving L * Y00^2 * 4*pi = L.
        assert coefficients[0] == pytest.approx(0.5 * 0.282095 * 4.0 * math.pi, abs=0.02)
        for k in range(3, 27):
            assert abs(coefficients[k]) < 0.02

    def test_dc_reconstruction_matches_the_integral(self):
        """Both paths must yield E/pi, or the runtime's per-normal ambient and its 3-zone
        fallback would disagree with each other."""
        radiance = sky.flat_sky((0.3, 0.6, 0.9))
        coefficients = sky.project_irradiance_sh(radiance)
        integrated = sky.integrate_irradiance(UP, radiance)
        for channel in range(3):
            reconstructed = coefficients[channel] * 0.282095
            assert reconstructed == pytest.approx(integrated[channel], abs=0.02)

    def test_gradient_sky_produces_a_nonzero_vertical_term(self):
        radiance = sky.godot_procedural_sky(
            (1.0, 1.0, 1.0), (1.0, 1.0, 1.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 1.0, 1.0
        )
        coefficients = sky.project_irradiance_sh(radiance)
        # Y1-1 is the y-linear band: a bright-above/dark-below sky must excite it.
        assert abs(coefficients[3]) > 0.1

    def test_coefficients_may_be_negative(self):
        """Why the contract stores these as full-precision floats rather than Color32."""
        radiance = sky.godot_procedural_sky(
            (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (1.0, 1.0, 1.0), 1.0, 1.0
        )
        assert any(c < 0.0 for c in sky.project_irradiance_sh(radiance))
