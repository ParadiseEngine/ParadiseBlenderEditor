"""Tests for the Blender Z-up -> contract Y-up basis change.

These pin the most consequential math in the addon. A regression here does not crash
anything -- it silently rotates every object in every exported scene by 90 degrees.
"""

from __future__ import annotations

import math

import pytest

from paradise_blender.contract import axes


def _approx(actual, expected, tol=1e-9):
    assert len(actual) == len(expected)
    for a, e in zip(actual, expected, strict=True):
        assert a == pytest.approx(e, abs=tol)


class TestConvertPoint:
    def test_maps_blender_up_to_contract_up(self):
        # Blender +Z (up) must become contract +Y (up).
        _approx(axes.convert_point((0.0, 0.0, 1.0)), (0.0, 1.0, 0.0))

    def test_maps_blender_forward_to_contract_forward(self):
        # Blender -Y is "forward" (the direction a default camera looks along);
        # the contract's forward is -Z.
        _approx(axes.convert_point((0.0, -1.0, 0.0)), (0.0, 0.0, 1.0))
        _approx(axes.convert_point((0.0, 1.0, 0.0)), (0.0, 0.0, -1.0))

    def test_right_is_shared(self):
        # +X is right in both bases -- the rotation is about X, so it is fixed.
        _approx(axes.convert_point((1.0, 0.0, 0.0)), (1.0, 0.0, 0.0))

    def test_preserves_length(self):
        converted = axes.convert_point((3.0, -4.0, 12.0))
        assert math.dist((0, 0, 0), converted) == pytest.approx(13.0)


class TestConvertPointInverse:
    def test_round_trips_through_convert_point(self):
        # The navmesh preview depends on this being an exact inverse: bake output (contract
        # axes) flows back into Blender through it.
        for point in [(0.0, 0.0, 1.0), (1.0, 2.0, 3.0), (-3.5, 0.25, -12.0)]:
            _approx(axes.convert_point_inverse(axes.convert_point(point)), point)
            _approx(axes.convert_point(axes.convert_point_inverse(point)), point)

    def test_maps_contract_up_to_blender_up(self):
        _approx(axes.convert_point_inverse((0.0, 1.0, 0.0)), (0.0, 0.0, 1.0))


class TestConvertMatrix:
    def test_identity_is_fixed(self):
        result = axes.convert_matrix(axes.identity())
        for row in range(4):
            _approx(result[row], axes.identity()[row])

    def test_translation_matches_point_conversion(self):
        translation = ((1, 0, 0, 2.0), (0, 1, 0, 3.0), (0, 0, 1, 5.0), (0, 0, 0, 1))
        result = axes.convert_matrix(translation)
        moved = (result[0][3], result[1][3], result[2][3])
        _approx(moved, axes.convert_point((2.0, 3.0, 5.0)))

    def test_conjugation_preserves_composition(self):
        """The property that makes conjugation the right choice over ``C @ M``.

        A parent/child hierarchy is a matrix product. If the conversion did not distribute
        over multiplication, converting parent and child separately would disagree with
        converting the composed world transform -- which is precisely how a hierarchy comes
        out mangled.
        """
        parent = _rotation_z(0.7)
        child = _translation(1.0, 2.0, 3.0)
        composed_then_converted = axes.convert_matrix(axes.matmul(parent, child))
        converted_then_composed = axes.matmul(
            axes.convert_matrix(parent), axes.convert_matrix(child)
        )
        for row in range(4):
            _approx(composed_then_converted[row], converted_then_composed[row], tol=1e-12)

    def test_blender_z_rotation_becomes_contract_y_rotation(self):
        """A yaw in Blender (about Z, its up axis) must be a yaw in the contract (about Y)."""
        result = axes.convert_matrix(_rotation_z(math.pi / 2))
        expected = _rotation_y(math.pi / 2)
        for row in range(4):
            _approx(result[row], expected[row], tol=1e-12)

    def test_scale_is_permuted_not_changed(self):
        scale = ((2.0, 0, 0, 0), (0, 3.0, 0, 0), (0, 0, 5.0, 0), (0, 0, 0, 1))
        result = axes.convert_matrix(scale)
        # Blender's Y and Z scale swap roles; magnitudes are untouched.
        assert result[0][0] == pytest.approx(2.0)
        assert result[1][1] == pytest.approx(5.0)
        assert result[2][2] == pytest.approx(3.0)

    def test_determinant_is_preserved(self):
        """Conjugation by a rotation cannot mirror -- so winding/chirality is safe."""
        m = axes.matmul(_rotation_z(0.4), _translation(1, 2, 3))
        assert _det3(axes.convert_matrix(m)) == pytest.approx(_det3(m))


class TestConvertQuaternion:
    def test_identity_is_fixed(self):
        _approx(axes.convert_quaternion((0.0, 0.0, 0.0, 1.0)), (0.0, 0.0, 0.0, 1.0))

    def test_agrees_with_matrix_conversion(self):
        """Quaternion and matrix paths must produce the same rotation.

        The exporter uses the quaternion path for entity rotations and the matrix path for
        world matrices; if these diverged, an entity's ``LocalRotation`` and ``LocalMatrix``
        would describe different orientations in the same document.
        """
        from paradise_blender.contract.matrix import quaternion_to_matrix

        angle = 0.9
        axis = _normalize((0.3, -0.5, 0.81))
        q = _quat_from_axis_angle(axis, angle)

        via_quaternion = quaternion_to_matrix(axes.convert_quaternion(q))
        via_matrix = axes.convert_matrix(quaternion_to_matrix(q))
        for row in range(3):
            _approx(via_quaternion[row][:3], via_matrix[row][:3], tol=1e-12)

    def test_angle_is_unchanged(self):
        q = _quat_from_axis_angle(_normalize((1.0, 2.0, -3.0)), 1.1)
        assert axes.convert_quaternion(q)[3] == pytest.approx(q[3])


class TestConvertEuler:
    def test_zero_rotation(self):
        _approx(axes.convert_euler_zyx_degrees((0.0, 0.0, 0.0)), (0.0, 0.0, 0.0), tol=1e-9)

    def test_blender_yaw_becomes_contract_yaw_in_degrees(self):
        # 90 degrees about Blender's Z (up) is 90 degrees about the contract's Y (up).
        result = axes.convert_euler_zyx_degrees((0.0, 0.0, math.pi / 2))
        _approx(result, (0.0, 90.0, 0.0), tol=1e-6)

    def test_round_trips_through_matrix(self):
        euler = (0.3, -0.4, 1.2)
        degrees = axes.convert_euler_zyx_degrees(euler)
        rebuilt = axes._euler_xyz_to_matrix(tuple(math.radians(d) for d in degrees))
        expected = axes.convert_matrix(axes._euler_xyz_to_matrix(euler))
        for row in range(3):
            _approx(rebuilt[row][:3], expected[row][:3], tol=1e-9)


def test_triangle_winding_is_not_flipped():
    """Documents the reasoning as an executable assertion: the conversion is a proper
    rotation, so navmesh and collision winding pass through untouched."""
    assert axes.convert_triangle_indices([0, 1, 2, 3, 4, 5]) == [0, 1, 2, 3, 4, 5]


# -- helpers -----------------------------------------------------------------------------


def _translation(x, y, z):
    return ((1, 0, 0, x), (0, 1, 0, y), (0, 0, 1, z), (0, 0, 0, 1))


def _rotation_z(angle):
    c, s = math.cos(angle), math.sin(angle)
    return ((c, -s, 0, 0), (s, c, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1))


def _rotation_y(angle):
    c, s = math.cos(angle), math.sin(angle)
    return ((c, 0, s, 0), (0, 1, 0, 0), (-s, 0, c, 0), (0, 0, 0, 1))


def _det3(m):
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


def _normalize(v):
    length = math.sqrt(sum(c * c for c in v))
    return tuple(c / length for c in v)


def _quat_from_axis_angle(axis, angle):
    half = angle / 2.0
    s = math.sin(half)
    return (axis[0] * s, axis[1] * s, axis[2] * s, math.cos(half))
