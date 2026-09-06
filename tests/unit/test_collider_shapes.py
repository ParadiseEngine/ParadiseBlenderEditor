"""A shape Empty's transform <-> the document's HostShape geometry, without Blender."""

from __future__ import annotations

import math

import pytest

from paradise_assets.document import collider_shapes


def close(a, b, eps=1e-6):
    return all(abs(x - y) <= eps for x, y in zip(a, b, strict=True))


def test_a_box_rides_on_the_scale_and_round_trips():
    row = {"ShapeType": "Box", "LocalCenter": [1, 2, 3], "LocalRotation": [0, 0, 0, 1],
           "Size": [2, 4, 6]}
    display, size, position, rotation, scale = collider_shapes.to_gizmo(row)
    assert (display, size) == ("CUBE", 0.5)
    # Document Y-up to Blender Z-up: (x, y, z) shows as (x, -z, y), extents likewise.
    assert close(position, (1, -3, 2))
    assert close(scale, (2, 6, 4))

    back = collider_shapes.from_gizmo("Box", size, position, rotation, scale)
    assert back["ShapeType"] == "Box"
    assert close(back["LocalCenter"], (1, 2, 3))
    assert close(back["Size"], (2, 4, 6))
    assert "Radius" not in back and "Height" not in back   # a box says nothing about them


def test_a_sphere_is_its_display_size_and_a_stretched_one_stays_enclosing():
    row = {"ShapeType": "Sphere", "Radius": 0.7}
    display, size, position, rotation, scale = collider_shapes.to_gizmo(row)
    assert (display, size) == ("SPHERE", 0.7)
    assert close(scale, (1, 1, 1))
    stretched = collider_shapes.from_gizmo("Sphere", 0.7, position, rotation, (1, 3, 1))
    assert stretched["Radius"] == pytest.approx(2.1)


def test_a_capsule_is_y_aligned_in_the_document_and_z_in_blender():
    row = {"ShapeType": "Capsule", "Radius": 0.3, "Height": 1.8}
    display, size, position, rotation, scale = collider_shapes.to_gizmo(row)
    assert display == "CUBE"
    assert close(scale, (0.6, 0.6, 1.8))          # the height is Blender Z
    back = collider_shapes.from_gizmo("Capsule", size, position, rotation, scale)
    assert back["Radius"] == pytest.approx(0.3) and back["Height"] == pytest.approx(1.8)
    assert "Size" not in back


def test_an_unused_member_keeps_whatever_the_document_had():
    stored = {"ShapeType": "Sphere", "Radius": 0.5, "Size": [1, 1, 1]}
    _, size, position, rotation, scale = collider_shapes.to_gizmo(stored)
    computed = collider_shapes.from_gizmo("Sphere", size, position, rotation, scale)
    merged = dict(stored)
    merged.update(collider_shapes.keep_unchanged(stored, computed))
    assert merged["Size"] == [1, 1, 1]


def test_a_rotated_box_round_trips_its_rotation():
    half = math.sqrt(0.5)
    row = {"ShapeType": "Box", "LocalRotation": [0, half, 0, half], "Size": [1, 2, 3]}
    _, size, position, rotation, scale = collider_shapes.to_gizmo(row)
    back = collider_shapes.from_gizmo("Box", size, position, rotation, scale)
    dot = abs(sum(a * b for a, b in zip(back["LocalRotation"], row["LocalRotation"], strict=True)))
    assert dot == pytest.approx(1.0, abs=1e-6)
    assert close(back["Size"], (1, 2, 3), 1e-5)


def test_an_unmoved_shape_keeps_the_documents_own_numbers():
    stored = {"ShapeType": "Box", "LocalCenter": [0, 0.25, 0], "LocalRotation": [0, 0, 0, 1],
              "Size": [2, 1.6, 4], "Radius": 0.0, "Height": 0.0, "IsTrigger": True}
    _, size, position, rotation, scale = collider_shapes.to_gizmo(stored)
    computed = collider_shapes.from_gizmo("Box", size, position, rotation, scale)
    kept = collider_shapes.keep_unchanged(stored, computed)
    assert kept["LocalCenter"] is stored["LocalCenter"]
    assert kept["Size"] is stored["Size"]
    assert "IsTrigger" not in kept                   # only geometry is the Empty's to say

    moved = collider_shapes.from_gizmo("Box", size, (5, 0, 0), rotation, scale)
    assert collider_shapes.keep_unchanged(stored, moved)["LocalCenter"] == [5.0, 0.0, 0.0]


def test_only_a_list_of_rows_with_a_host_shape_member_is_a_shape_array():
    class Field:
        def __init__(self, type_, items=None, authored_by=None, fields=(), name=""):
            self.type, self.items, self.authored_by = type_, items, authored_by
            self.fields, self.name = list(fields), name
    shape = Field("object", authored_by="shape", name="Shape")
    flag = Field("bool", name="IsTrigger")
    rows = Field("array", Field("object", fields=[shape, flag]))
    assert collider_shapes.is_shape_array(rows)
    assert collider_shapes.host_member(rows) is shape
    assert not collider_shapes.is_shape_array(Field("array", Field("object", fields=[flag])))
    assert not collider_shapes.is_shape_array(
        Field("array", Field("object", fields=[Field("object", authored_by="light")])))
    assert not collider_shapes.is_shape_array(Field("object", fields=[shape]))
    # The row may BE the shape: then there is no member to look under.
    bare = Field("array", Field("object", authored_by="shape", name="Value"))
    assert collider_shapes.is_shape_array(bare) and collider_shapes.member_name(bare) is None
    assert collider_shapes.member_name(rows) == "Shape"
    assert collider_shapes.is_shape_single(Field("object", authored_by="shape", name="Volume"))
