from __future__ import annotations

import math

import pytest

from paradise_assets.document.component_schema import ComponentSchema, Vocabulary
from paradise_assets.document.light_preview import describe


def vocabulary(kind="Spot"):
    components = {
        "kind": ComponentSchema({"id": "kind", "previewLight": kind}),
        "params": ComponentSchema(
            {
                "id": "params",
                "fields": [
                    {"name": "Tint", "type": "color", "lightField": "Color"},
                    {"name": "Brightness", "type": "float", "lightField": "Intensity", "default": 1},
                    {"name": "Reach", "type": "float", "lightField": "Range"},
                    {"name": "Radius", "type": "float", "lightField": "Size"},
                    {"name": "Cone", "type": "float", "lightField": "OuterDegrees", "default": 45},
                    {"name": "Core", "type": "float", "lightField": "InnerDegrees"},
                    {"name": "Casts", "type": "bool", "lightField": "Shadows"},
                ],
            }
        ),
    }
    return Vocabulary(components, "test")


def test_fields_are_bound_by_explicit_semantics_instead_of_clr_names():
    plan = describe(
        [
            {"id": "kind"},
            {
                "id": "params",
                "data": {
                    "Tint": {"r": 1, "g": 0.5, "b": 0.25, "a": 1},
                    "Brightness": 3,
                    "Reach": 12,
                    "Radius": 0.4,
                    "Cone": 70,
                    "Core": 35,
                    "Casts": True,
                },
            },
        ],
        vocabulary(),
    )
    assert plan.kind == "Spot"
    assert plan.color == pytest.approx((1, 0.21404114, 0.05087609))
    assert plan.energy == 300
    assert plan.range == 12
    assert plan.radius == 0.4
    assert plan.outer == pytest.approx(math.radians(70))
    assert plan.blend == 0.5
    assert plan.shadows


def test_sun_uses_native_irradiance_and_punctual_uses_100_watts_per_unit():
    for kind, energy in [("Directional", 1), ("Point", 100), ("Spot", 100)]:
        plan = describe([{"id": "kind"}, {"id": "params", "data": {}}], vocabulary(kind))
        assert plan.energy == energy


def test_preview_clamps_only_the_projection_not_authored_values():
    data = {"Cone": 240, "Core": 500, "Reach": -4}
    plan = describe([{"id": "kind"}, {"id": "params", "data": data}], vocabulary())
    assert plan.outer == pytest.approx(math.radians(179))
    assert plan.blend == 0
    assert plan.range == 0
    assert data == {"Cone": 240, "Core": 500, "Reach": -4}


def test_absent_marker_is_not_a_light_and_multiple_archetypes_are_refused():
    assert describe([{"id": "params", "data": {}}], vocabulary()) is None
    with pytest.raises(ValueError, match="exactly one"):
        describe([{"id": "kind"}, {"id": "kind"}], vocabulary())


def test_light_annotations_leave_canonical_fields_editable():
    schema = vocabulary().get("params")
    assert all(field.editable for field in schema.fields)


def test_nested_metadata_reads_nested_fields_and_their_defaults():
    types = vocabulary()
    types._components["nested"] = ComponentSchema(
        {
            "id": "nested",
            "fields": [
                {
                    "name": "Shape",
                    "type": "object",
                    "fields": [
                        {"name": "Width", "type": "float", "lightField": "OuterDegrees", "default": 75},
                        {"name": "Core", "type": "float", "lightField": "InnerDegrees", "default": 25},
                    ],
                },
            ],
        }
    )
    plan = describe([{"id": "kind"}, {"id": "nested", "data": {"Shape": {"Width": 100}}}], types)
    assert plan.outer == pytest.approx(math.radians(100))
    assert plan.blend == 0.75
    plan = describe([{"id": "kind"}, {"id": "nested", "data": {}}], types)
    assert plan.outer == pytest.approx(math.radians(75))


def test_non_light_objects_ignore_shared_parameter_components():
    assert describe([{"id": "params"}, {"id": "params"}], vocabulary()) is None


def test_colour_defaults_use_the_engine_rgba_contract():
    from paradise_assets.document.component_schema import FieldSchema

    for raw, expected in [
        ({}, dict(zip("rgba", [1, 1, 1, 1], strict=True))),
        ({"default": [0.2, 0.3, 0.4, 1]}, {"r": 0.2, "g": 0.3, "b": 0.4, "a": 1}),
    ]:
        assert FieldSchema({"type": "color", **raw}).default_value() == expected
