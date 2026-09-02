"""Tests for reading the game's authoring schema as a vocabulary of editable fields.

The stakes are narrow but real: this decides which fields the Components panel offers an editor
for. Offering one it should not is the failure that matters -- a host-authored field typed in by
hand is authoring in the place the export overwrites, and the value disappears on the next build
with nothing to say it did.
"""

from __future__ import annotations

import json
import os
import tempfile

from paradise_assets.document import component_schema, well_known


def _project(components) -> str:
    """A throwaway project root carrying a dumped schema, as the game's build would leave it."""
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "build"), exist_ok=True)
    with open(os.path.join(root, "build", "authoring-schema.json"), "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "components": components}, handle)
    return root


def test_a_project_with_no_dump_has_no_vocabulary():
    # A fresh clone has never built the game. That is a normal state, not an error: the panel
    # says "build the game to edit fields" rather than reporting the addon as broken.
    vocabulary = component_schema.load(tempfile.mkdtemp())

    assert not vocabulary
    assert vocabulary.source is None
    assert vocabulary.get("anything") is None


def test_fields_carry_their_type_doc_and_range():
    root = _project([{
        "id": "11111111-1111-4111-8111-111111111111",
        "type": "Game.Speed",
        "displayName": "Speed",
        "fields": [
            {"name": "MaxSpeed", "type": "float", "doc": "How fast.",
             "minimum": 0.0, "maximum": 10.0, "unit": "m/s", "default": 4.0},
        ],
    }])

    field = component_schema.load(root).get("11111111-1111-4111-8111-111111111111").field("MaxSpeed")

    assert field.type == "float"
    assert field.doc == "How fast."
    assert field.unit == "m/s"
    assert field.default == 4.0
    assert field.editable


def test_a_host_authored_field_is_not_editable():
    # THE one that matters. `[AuthoredByHost]` means the value is derived from the host object --
    # the mesh it points at, the shape drawn with Blender's handles -- so the export overwrites
    # whatever is typed. Its type is `string`, which is editable, so only the flag stops it.
    root = _project([{
        "id": "22222222-2222-4222-8222-222222222222",
        "type": "Game.Mesh",
        "fields": [{"name": "Mesh", "type": "string", "authoredBy": "mesh",
                    "assetKinds": [".glb"]}],
    }])

    field = component_schema.load(root).get("22222222-2222-4222-8222-222222222222").field("Mesh")

    assert field.type in component_schema.EDITABLE_TYPES
    assert field.authored_by == "mesh"
    assert not field.editable


def test_nested_records_flatten_to_editable_paths():
    # Not a refusal -- they used to be, because the panel addressed fields by name. A slash path
    # is what lets Camera/Guide/NearDistance be the same kind of edit as MaxSpeed.
    root = _project([{
        "id": "33333333-3333-4333-8333-333333333333",
        "type": "Game.Nested",
        "fields": [
            {"name": "Group", "type": "object", "fields": [
                {"name": "Speed", "type": "float", "default": 1.0},
            ]},
            {"name": "Slots", "type": "array", "items": {"type": "string"}},
        ],
    }])

    schema = component_schema.load(root).get("33333333-3333-4333-8333-333333333333")
    plan = schema.plan({"Group": {"Speed": 4.0}, "Slots": ["a", "b"]})

    by_path = {item.path: item for item in plan}
    assert by_path["Group/Speed"].role == component_schema.ROLE_LEAF
    assert by_path["Group/Speed"].field.editable
    assert by_path["Slots"].role == component_schema.ROLE_ARRAY
    assert by_path["Slots/0"].role == component_schema.ROLE_ROW
    assert by_path["Slots/1"].role == component_schema.ROLE_ROW
    assert schema.resolve("Group/Speed").type == "float"
    assert schema.resolve("Slots/0").type == "string"


def test_a_host_authored_list_stays_locked():
    root = _project([{
        "id": "44444444-4444-4444-8444-444444444444",
        "type": "Game.Collider",
        "fields": [{
            "name": "Shapes",
            "type": "array",
            "items": {"type": "object", "authoredBy": "shape", "fields": [
                {"name": "Size", "type": "vector3"},
            ]},
        }],
    }])

    schema = component_schema.load(root).get("44444444-4444-4444-8444-444444444444")
    plan = schema.plan({"Shapes": [{"Size": [1, 1, 1]}]})

    assert len(plan) == 1
    assert plan[0].role == component_schema.ROLE_LOCKED
    assert not plan[0].field.editable


def test_the_formats_own_components_are_refused_even_if_dumped():
    # meta and transform have CLOSED schemas the format owns, and Blender's name field and gizmo
    # are their editor. A game whose dump described them -- by accident or by a stale build --
    # must not get a second way to type in an identity or a placement.
    root = _project([
        {"id": well_known.META_ID, "type": "meta", "fields": [{"name": "Name", "type": "string"}]},
        {"id": well_known.TRANSFORM_ID, "type": "transform",
         "fields": [{"name": "Position", "type": "vector3"}]},
    ])

    vocabulary = component_schema.load(root)

    assert vocabulary.get(well_known.META_ID) is None
    assert vocabulary.get(well_known.TRANSFORM_ID) is None
    assert not vocabulary


def test_lookup_is_case_insensitive_on_the_id():
    # A guid is hex, and nothing in the format promises a case. Matching exactly would make a
    # component editable or not depending on how its id happened to be spelled.
    root = _project([{"id": "AABBCCDD-1111-4111-8111-111111111111", "type": "Game.X",
                      "fields": [{"name": "Value", "type": "int"}]}])

    vocabulary = component_schema.load(root)

    assert vocabulary.get("aabbccdd-1111-4111-8111-111111111111") is not None
    assert vocabulary.get("AABBCCDD-1111-4111-8111-111111111111") is not None


def test_an_unreadable_dump_reads_as_no_vocabulary():
    # It is a build product; a half-written one must not stop a document opening.
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "build"), exist_ok=True)
    with open(os.path.join(root, "build", "authoring-schema.json"), "w", encoding="utf-8") as handle:
        handle.write("{ not json")

    assert not component_schema.load(root)


def test_clamp_holds_a_value_to_the_declared_range():
    field = component_schema.FieldSchema(
        {"name": "X", "type": "float", "minimum": 0.0, "maximum": 1.0})

    assert field.clamp(2.0) == 1.0
    assert field.clamp(-1.0) == 0.0
    assert field.clamp(0.5) == 0.5
    # A bool is not a number to clamp, even though Python says it is one.
    assert field.clamp(True) is True


def test_field_caption_puts_kg_on_the_label_and_leaves_widget_units_off():
    # Kilograms has no Blender ID-property subtype, so the unit has to live on the label or it
    # is invisible. Metres/radians/seconds/unit01 become the widget itself (distance spinner,
    # angle, time, 0-1 factor) and must not also say "(meters)" next to that.
    assert component_schema.field_caption("Mass", "kilograms") == "Mass (kg)"
    assert component_schema.field_caption("Near", "meters") == "Near"
    assert component_schema.field_caption("Yaw", "radians") == "Yaw"
    assert component_schema.field_caption("Delay", "seconds") == "Delay"
    assert component_schema.field_caption("Friction", "unit01") == "Friction"
    assert component_schema.field_caption("Speed", "m/s") == "Speed (m/s)"
    assert component_schema.field_caption("Lives", None) == "Lives"


def test_unit01_and_a_closed_range_are_sliders():
    assert component_schema.has_slider(0.0, 1.0, "unit01")
    assert component_schema.has_slider(None, None, "unit01")
    assert component_schema.has_slider(0.001, 10000, "kilograms")
    assert not component_schema.has_slider(0.0, None, None)
    assert not component_schema.has_slider(None, None, None)


def test_numeric_widget_options_map_units_and_ranges():
    mass = component_schema.FieldSchema(
        {"name": "Mass", "type": "float", "unit": "kilograms",
         "minimum": 0.001, "maximum": 10000})
    friction = component_schema.FieldSchema(
        {"name": "Friction", "type": "float", "unit": "unit01"})
    near = component_schema.FieldSchema(
        {"name": "Near", "type": "float", "unit": "meters"})

    mass_opts = component_schema.numeric_widget_options(mass)
    assert mass_opts["min"] == 0.001
    assert mass_opts["max"] == 10000
    assert "subtype" not in mass_opts

    friction_opts = component_schema.numeric_widget_options(friction)
    assert friction_opts["subtype"] == "FACTOR"
    assert friction_opts["min"] == 0.0
    assert friction_opts["max"] == 1.0

    assert component_schema.numeric_widget_options(near)["subtype"] == "DISTANCE"
    assert component_schema.id_subtype("radians") == "ANGLE"
    assert component_schema.id_subtype("kilograms") is None


_RIGIDBODY = {
    "id": "b7ab4dd8-c8da-4dc2-9e5e-192fd74deb11",
    "type": "Paradise.Export.Data.RigidbodyComponentData",
    "data": {
        "BodyType": "Dynamic",
        "Mass": 1.0,
        "LinearDamping": 0.0,
        "Restitution": 0.2,
        "Friction": 0.5,
        "Layer": 0,
        "LayerName": "",
    },
}


def test_describe_offers_a_rigidbody_even_without_a_dump():
    # v6 dropped engine types from the dump, but prefabs still carry them. A panel that can
    # only print BodyType is the bug this form exists to close.
    schema = component_schema.describe(_RIGIDBODY, component_schema.Vocabulary({}, None))

    assert schema is not None
    assert schema.field("BodyType").type == "enum"
    assert schema.field("BodyType").values == ["None", "Static", "Kinematic", "Dynamic"]
    assert schema.field("Mass").type == "float"
    assert schema.field("Mass").editable
    assert schema.plan(_RIGIDBODY["data"])[0].role == component_schema.ROLE_LEAF
    assert {item.path for item in schema.plan(_RIGIDBODY["data"])} >= {
        "BodyType", "Mass", "LinearDamping",
    }
    hidden = schema.plan({**_RIGIDBODY["data"], "BodyType": "Static"})
    assert "Mass" not in {item.path for item in hidden}


def test_an_asset_reference_stays_editable():
    # `authoredBy: asset` is a FILE picker, not a host-object bake. Locking it was the bug that
    # made every material slot a static label.
    root = _project([{
        "id": "55555555-5555-4555-8555-555555555555",
        "type": "Game.Materials",
        "fields": [{
            "name": "Slots",
            "type": "array",
            "items": {"type": "string", "authoredBy": "asset", "assetKinds": [".toml"]},
        }],
    }])
    schema = component_schema.load(root).get("55555555-5555-4555-8555-555555555555")
    payload = {"Slots": [{"guid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                          "path": "materials/car.toml"}]}
    plan = schema.plan(payload)

    assert schema.field("Slots").items.editable
    by_path = {item.path: item for item in plan}
    assert by_path["Slots"].role == component_schema.ROLE_ARRAY
    assert by_path["Slots/0"].role == component_schema.ROLE_ROW
    assert component_schema.is_asset_field(by_path["Slots/0"].field, payload["Slots"][0])
    assert "guid" not in by_path


def test_asset_kinds_on_an_array_apply_to_each_row():
    # The generator writes [AuthorAssetKinds] on the LIST, not on items. Rows still have to be
    # pickers, or every material slot is a string box again.
    root = _project([{
        "id": "66666666-6666-4666-8666-666666666666",
        "type": "Game.Materials",
        "fields": [{
            "name": "Slots",
            "type": "array",
            "assetKinds": [".toml"],
            "items": {"type": "string"},
        }],
    }])
    schema = component_schema.load(root).get("66666666-6666-4666-8666-666666666666")

    assert schema.field("Slots").items.asset_kinds == [".toml"]
    assert component_schema.is_asset_field(schema.field("Slots").items)
    assert not component_schema.is_asset_field(schema.field("Slots"))

    payload = {"Slots": [
        {"guid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "path": "materials/a.toml"},
        {},
    ]}
    plan = schema.plan(payload)
    by_path = {item.path: item for item in plan}
    assert by_path["Slots"].role == component_schema.ROLE_ARRAY
    assert by_path["Slots/0"].role == component_schema.ROLE_ROW
    assert by_path["Slots/1"].role == component_schema.ROLE_ROW
    assert component_schema.default_payload(schema) == {"Slots": []}


def test_describe_refuses_meta_and_transform():
    vocabulary = component_schema.Vocabulary({}, None)

    assert component_schema.describe(
        {"id": well_known.META_ID, "type": "meta", "data": {"Name": "Car"}},
        vocabulary,
    ) is None
    assert component_schema.describe(
        {"id": well_known.TRANSFORM_ID, "type": "transform",
         "data": {"Position": [0, 1, 0]}},
        vocabulary,
    ) is None
    assert component_schema.is_format_owned(well_known.META_ID)
    assert component_schema.is_format_owned(well_known.TRANSFORM_ID)


def test_describe_does_not_invent_an_editor_for_host_derived_engine_types():
    # A mesh path typed into a form is authoring in the place the bake overwrites.
    schema = component_schema.describe(
        {"id": "f2c0357e-94dd-4a5a-9803-518066cb54b2",
         "type": "Paradise.Export.Data.RenderableComponentData",
         "data": {"Mesh": "meshes/car.glb"}},
        component_schema.Vocabulary({}, None),
    )

    assert schema is None


def test_describe_prefers_the_dump_over_inference():
    root = _project([{
        "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "type": "Game.Custom",
        "fields": [{"name": "Count", "type": "int", "default": 1}],
    }])
    vocabulary = component_schema.load(root)

    schema = vocabulary.describe({
        "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "data": {"Count": 3, "Extra": "ignored by the dump"},
    })

    assert schema.field("Count") is not None
    assert schema.field("Extra") is None


def test_infer_makes_unknown_payloads_editable():
    schema = component_schema.infer({
        "id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "type": "Game.Unknown",
        "data": {
            "Enabled": True,
            "Count": 3,
            "Speed": 1.5,
            "Title": "hi",
            "Offset": [1.0, 2.0, 3.0],
        },
    })

    assert schema.field("Enabled").type == "bool"
    assert schema.field("Count").type == "int"
    assert schema.field("Speed").type == "float"
    assert schema.field("Title").type == "string"
    assert schema.field("Offset").type == "vector3"
    assert all(item.role == component_schema.ROLE_LEAF for item in schema.plan({}))


def test_format_value_prints_numeric_lists_as_vectors():
    # The panel used to summarise these as `[3 item(s)]`, which is a count, not a value.
    assert component_schema.format_value([0.0, 0.55, 38.0]) == "(0, 0.55, 38)"
    assert component_schema.format_value([0.0, 0.0, 0.0, 1.0]) == "(0, 0, 0, 1)"
    assert component_schema.format_value(["a", "b"]) == "[2 item(s)]"


def test_parent_caption_pairs_the_name_with_the_identity():
    guid = "3eb3ba62-90b1-5f55-a6be-4240d7ae552a"

    assert well_known.parent_caption(guid, "Highway") == f"Highway  ({guid})"
    assert well_known.parent_caption(guid, None) == guid
    assert well_known.parent_caption(None, "Highway") == "— (root)"
    assert well_known.parent_caption("", "") == "— (root)"


def test_addable_offers_dump_types_and_rigidbody_but_not_host_derived():
    marker_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    root = _project([{
        "id": marker_id,
        "type": "Game.Marker",
        "displayName": "Car Marker",
        "fields": [],
    }])
    vocabulary = component_schema.load(root)
    offered = {schema.id.lower() for schema in component_schema.addable(vocabulary, [])}

    assert marker_id in offered
    assert "b7ab4dd8-c8da-4dc2-9e5e-192fd74deb11" in offered
    assert "f2c0357e-94dd-4a5a-9803-518066cb54b2" not in offered
    assert well_known.META_ID.lower() not in offered
    assert well_known.TRANSFORM_ID.lower() not in offered

    remaining = component_schema.addable(vocabulary, [marker_id])
    assert all(schema.id.lower() != marker_id for schema in remaining)


def test_default_payload_uses_schema_defaults():
    schema = component_schema.describe(
        {"id": "b7ab4dd8-c8da-4dc2-9e5e-192fd74deb11", "data": {}},
        component_schema.Vocabulary({}, None),
    )

    payload = component_schema.default_payload(schema)

    assert payload["BodyType"] == "Dynamic"
    assert payload["Mass"] == 1.0
    assert "LinearDamping" in payload
