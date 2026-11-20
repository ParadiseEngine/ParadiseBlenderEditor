"""The ``HostSpline`` bake as numbers (#76, ParadiseEngine#362), without Blender.

The contract under test: knots and RELATIVE handles in document axes, object space, in order; a
cyclic flag; a copy rather than a resample; refusal of what the document cannot carry; and the
fixed point -- re-deriving an unmoved curve keeps the document's own spelling.
"""

from __future__ import annotations

import math
import struct

import pytest

from paradise_assets.document import component_schema, splines


def _schema(fields: list[dict]) -> component_schema.ComponentSchema:
    return component_schema.ComponentSchema({"id": "route", "displayName": "Route", "fields": fields})


SPLINE_ROW = {"type": "object", "authoredBy": "spline", "fields": [
    {"name": "Knots", "type": "array", "items": {"type": "object", "fields": [
        {"name": "Position", "type": "vector3"},
        {"name": "HandleIn", "type": "vector3"},
        {"name": "HandleOut", "type": "vector3"},
    ]}},
    {"name": "Cyclic", "type": "bool"},
]}


def test_handles_are_offsets_in_document_axes():
    # Blender (x, y, z) is the document's (x, z, -y).
    row = splines.from_blender("Rail", [
        ((1.0, 2.0, 3.0), (0.0, 2.0, 3.0), (2.0, 2.0, 3.0)),
        ((5.0, 0.0, 0.0), (5.0, -1.0, 0.0), (5.0, 1.0, 0.5)),
    ], cyclic=False)
    assert row == {
        "Knots": [
            {"Position": [1.0, 3.0, -2.0], "HandleIn": [-1.0, 0.0, 0.0], "HandleOut": [1.0, 0.0, 0.0]},
            {"Position": [5.0, 0.0, 0.0], "HandleIn": [0.0, 0.0, 1.0], "HandleOut": [0.0, 0.5, -1.0]},
        ],
        "Cyclic": False,
    }


def test_round_trip_through_blender_axes():
    points = [
        ((0.25, -1.5, 2.0), (0.0, -1.0, 2.0), (0.5, -2.0, 2.0)),
        ((3.0, 4.0, -5.0), (2.0, 4.0, -5.5), (4.0, 4.0, -4.5)),
        ((-1.0, 0.0, 0.0), (-1.0, 1.0, 0.0), (-1.0, -1.0, 0.0)),
    ]
    row = splines.from_blender("Loop", points, cyclic=True)
    back, cyclic = splines.to_blender(row)
    assert cyclic is True
    for (co, left, right), (bco, bleft, bright) in zip(points, back, strict=True):
        for a, b in ((co, bco), (left, bleft), (right, bright)):
            assert all(math.isclose(x, y, abs_tol=1e-9) for x, y in zip(a, b, strict=True))


def test_moving_a_knot_moves_its_handles_without_skewing_them():
    last = ((4, 0, 0), (3, 0, 0), (5, 0, 0))
    before = splines.from_blender("R", [((0, 0, 0), (-1, 0, 0), (1, 0, 0)), last], False)
    after = splines.from_blender("R", [((0, 0, 7), (-1, 0, 7), (1, 0, 7)), last], False)
    assert before["Knots"][0]["HandleIn"] == after["Knots"][0]["HandleIn"]
    assert before["Knots"][0]["Position"] != after["Knots"][0]["Position"]


def test_too_few_knots_and_non_finite_coordinates_are_refused_by_name():
    with pytest.raises(splines.SplineError, match=r"Rail .* at least 2 knots"):
        splines.from_blender("Rail (spline 0)", [((0, 0, 0), (0, 0, 0), (0, 0, 0))], False)
    with pytest.raises(splines.SplineError, match="knot 1 has a coordinate that is not a finite"):
        splines.from_blender("Rail", [((0, 0, 0),) * 3, ((math.nan, 0, 0), (0, 0, 0), (0, 0, 0))], False)


def test_float32_noise_keeps_the_documents_spelling():
    stored = {"Knots": [{"Position": [0.3, 1.0, -2.0], "HandleIn": [0.1, 0.0, 0.0],
                         "HandleOut": [-0.1, 0.0, 0.0], "Weight": 2.0}], "Cyclic": False}
    noisy = {"Knots": [{"Position": [0.30000001, 1.0, -2.0], "HandleIn": [0.1000001, 0.0, 0.0],
                        "HandleOut": [-0.1, 0.0, 0.0]}], "Cyclic": False}
    kept = splines.keep_unchanged(stored, noisy)
    assert kept == stored   # including a member a later kind may add
    moved = {"Knots": [{"Position": [0.5, 1.0, -2.0], "HandleIn": [0.1, 0.0, 0.0],
                        "HandleOut": [-0.1, 0.0, 0.0]}], "Cyclic": True}
    changed = splines.keep_unchanged(stored, moved)
    assert changed["Knots"][0]["Position"] == [0.5, 1.0, -2.0]
    assert changed["Knots"][0]["HandleIn"] == [0.1, 0.0, 0.0]
    assert changed["Cyclic"] is True


def _f32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def test_a_curve_far_from_the_origin_round_trips_through_float32_unchanged():
    # Blender keeps handles ABSOLUTE in float32: at x=1000 its precision is ~6e-5, far coarser
    # than a 0.1 handle offset's own magnitude would allow for.
    stored = {"Knots": [
        {"Position": [1000.0, 2.0, -3.0], "HandleIn": [-0.1, 0.0, 0.0], "HandleOut": [0.1, 0.0, 0.0]},
        {"Position": [1001.0, 2.0, -3.0], "HandleIn": [-0.1, 0.0, 0.0], "HandleOut": [0.1, 0.0, 0.0]},
    ], "Cyclic": False}
    points, cyclic = splines.to_blender(stored)
    as_float32 = [tuple(tuple(_f32(c) for c in v) for v in point) for point in points]
    computed = splines.from_blender("Far", as_float32, cyclic)
    assert computed != stored            # the noise is real
    assert splines.keep_unchanged(stored, computed) == stored
    assert splines.same_geometry(stored, computed)
    moved = splines.from_blender("Far", [as_float32[0], tuple(
        (x + 0.5, y, z) for x, y, z in as_float32[1])], cyclic)
    assert not splines.same_geometry(stored, moved)


def test_same_geometry_compares_knot_count_and_cyclic():
    row = splines.from_blender("R", [((0, 0, 0),) * 3, ((1, 0, 0),) * 3], False)
    assert splines.same_geometry(row, dict(row))
    assert not splines.same_geometry(row, dict(row, Cyclic=True))
    assert not splines.same_geometry(row, {"Knots": row["Knots"][:1], "Cyclic": False})
    assert not splines.same_geometry(None, row)


def test_an_overlay_row_holding_a_nested_record_restores_to_header_rows():
    # A duplicated curve's components come back from JSON with no table form; its rows hold a
    # spline record with a knot list, which no inline table may contain.
    from paradise_assets.document import canonical_toml

    payload = {"Legs": [{"Curve": {"Knots": [{"Position": [0.0, 0.0, 0.0]}], "Cyclic": False},
                         "Speed": 2.0}]}
    restored = canonical_toml.restore_inline_tables(payload, structural_rows=True)
    text = canonical_toml.dumps(restored)
    assert text.startswith("[[Legs]]\n")
    assert canonical_toml.loads(text) == payload
    with pytest.raises(ValueError, match="nests a table"):
        canonical_toml.restore_inline_tables(payload)


def test_negative_zero_is_never_written():
    row = splines.from_blender("R", [((0, 0, 0), (0, 0, 0), (0, 0, 0)), ((1, 0, 0),) * 3], False)
    assert all(math.copysign(1.0, v) > 0 for knot in row["Knots"] for v in knot["Position"])


def test_which_fields_are_splines():
    listed = _schema([{"name": "Paths", "type": "array", "items": dict(SPLINE_ROW, name="Paths")}])
    single = _schema([dict(SPLINE_ROW, name="Path")])
    record = _schema([{"name": "Legs", "type": "array", "items": {"type": "object", "fields": [
        dict(SPLINE_ROW, name="Curve"), {"name": "Speed", "type": "float"}]}}])
    plain = _schema([{"name": "Speed", "type": "float"}])

    assert [f.name for f in splines.spline_fields(listed)] == ["Paths"]
    assert splines.is_spline_array(listed.field("Paths"))
    assert splines.is_spline_single(single.field("Path"))
    assert splines.member_name(record.field("Legs")) == "Curve"
    assert splines.member_name(listed.field("Paths")) is None
    assert splines.spline_fields(plain) == []


def test_the_panel_locks_the_geometry_and_offers_the_game_members():
    record = _schema([{"name": "Legs", "type": "array", "items": {"type": "object", "fields": [
        dict(SPLINE_ROW, name="Curve"), {"name": "Speed", "type": "float"}]}}])
    data = {"Legs": [{"Curve": {"Knots": [], "Cyclic": False}, "Speed": 2.0},
                     {"Curve": {"Knots": [], "Cyclic": True}, "Speed": 3.0}]}
    plan = record.plan(data)
    assert [(item.path, item.role) for item in plan] == [
        ("Legs", component_schema.ROLE_SPLINES),
        ("Legs/0/Speed", component_schema.ROLE_LEAF),
        ("Legs/1/Speed", component_schema.ROLE_LEAF),
    ]
    # No Add/Remove: the curve decides how many rows there are.
    assert not record.field("Legs").editable


def test_a_bare_spline_list_is_one_locked_row():
    listed = _schema([{"name": "Paths", "type": "array", "items": dict(SPLINE_ROW, name="Paths")}])
    plan = listed.plan({"Paths": [{"Knots": [], "Cyclic": False}]})
    assert [(item.path, item.role) for item in plan] == [("Paths", component_schema.ROLE_SPLINES)]
    assert component_schema.is_host_locked(listed.field("Paths").items)
