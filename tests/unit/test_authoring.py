"""Tests for the authoring-schema reader and the Custom payload builder.

The reference implementation is the Godot host (``AuthoredEntityCore``): these tests pin the
same wire rules its ``ValueOf`` defines, so the two hosts cannot drift apart on what an
authored component looks like in an exported document.
"""

from __future__ import annotations

import json

import pytest

from paradise_blender.contract import authoring, component_ids, schema, writer

#: A game's dumped schema, as of v3: GUID ids, and a `type` beside each one.
CREATURE_ID = "1f0a4c62-8b3d-4e07-9c15-2a6d8f43be91"

PINGU_LIKE = json.dumps(
    {
        "version": 3,
        "components": [
            {
                "id": CREATURE_ID,
                "type": "Pingu.Core.Authoring.Creature",
                "displayName": "Creature",
                "fields": [
                    {"name": "MaxSpeed", "type": "float", "minimum": 0.1, "maximum": 50, "default": 7},
                    {"name": "Lives", "type": "int", "default": 3},
                    {"name": "Friendly", "type": "bool", "default": True},
                    {"name": "Nickname", "type": "string"},
                    {"name": "Title", "type": "string", "default": ""},
                    {"name": "Mode", "type": "enum", "values": ["Idle", "Chase", "Flee"], "default": "Chase"},
                    {"name": "Home", "type": "vector3", "default": [1, 2, 3]},
                    {"name": "Facing", "type": "quaternion"},
                    {"name": "Tint", "type": "color", "default": {"r": 1, "g": 0.5, "b": 0, "a": 1}},
                    {
                        "name": "Box",
                        "type": "object",
                        "fields": [
                            {"name": "SizeX", "type": "float", "default": 1},
                            {"name": "SizeY", "type": "float", "default": 2},
                        ],
                    },
                    {
                        "name": "Shape",
                        "type": "object",
                        "authoredBy": "shape",
                        "fields": [{"name": "SizeX", "type": "float"}],
                    },
                    {
                        "name": "Extras",
                        "type": "array",
                        "items": {"name": "", "type": "object", "authoredBy": "node"},
                    },
                    {
                        "name": "FlipChance",
                        "type": "float",
                        "visibleWhen": {"field": "Friendly", "equals": True},
                    },
                ],
            }
        ],
    }
)


def creature() -> authoring.AuthoredComponentSchema:
    return authoring.read(PINGU_LIKE).components[0]


class TestRead:
    def test_reads_ids_names_and_fields(self):
        component = creature()
        assert component.id == CREATURE_ID
        assert component.type == "Pingu.Core.Authoring.Creature"
        assert component.display_name == "Creature"
        assert component.fields[0].name == "MaxSpeed"
        assert component.fields[0].minimum == 0.1

    def test_display_name_falls_back_to_the_type_not_the_id(self):
        """A bare GUID is not a label. When nothing declared a display name the TYPE stands in,
        because it is the only member of a v3 component a human can read."""
        document = authoring.read(
            '{"version": 3, "components": [{"id": "' + CREATURE_ID + '",'
            ' "type": "Game.Thing"}]}'
        )
        assert document.components[0].display_name == "Game.Thing"

    def test_an_uppercase_id_is_normalized_on_the_way_in(self):
        """A hand-typed [Guid] in a game repo can arrive in any case. Left alone it would open a
        second storage namespace on the same object, invisible against the first."""
        document = authoring.read(
            '{"version": 3, "components": [{"id": "' + CREATURE_ID.upper() + '"}]}'
        )
        assert document.components[0].id == CREATURE_ID

    def test_rejects_a_newer_version_than_this_reader_understands(self):
        with pytest.raises(authoring.SchemaError, match="version 99"):
            authoring.read('{"version": 99, "components": []}')

    def test_rejects_a_v2_document_rather_than_guessing_at_its_names(self):
        """The case that actually happens: a game built before the ids became GUIDs. There is no
        way to derive a component's GUID from "paradise.rigidbody", so the document is refused
        and regenerated rather than half-read into ids that resolve to nothing."""
        with pytest.raises(authoring.SchemaError, match="older than"):
            authoring.read('{"version": 2, "components": []}')

    def test_rejects_malformed_json_loudly(self):
        """A schema an editor cannot read must not be papered over with an empty list -- the
        symptom would be "my component vanished" with no cause anywhere."""
        with pytest.raises(authoring.SchemaError, match="not valid JSON"):
            authoring.read("{not json")


class TestMerge:
    def test_earlier_sources_win_on_a_duplicate_id(self):
        rigidbody = component_ids.RIGIDBODY
        engine = authoring.read(
            '{"version": 3, "components": [{"id": "' + rigidbody + '",'
            ' "type": "Zzz.Rigidbody", "displayName": "Engine"}]}'
        )
        game = authoring.read(
            '{"version": 3, "components": [{"id": "' + rigidbody + '",'
            ' "type": "Zzz.Rigidbody", "displayName": "Impostor"},'
            ' {"id": "' + CREATURE_ID + '", "type": "Aaa.Own"}]}'
        )
        merged = authoring.merge([engine, game])
        assert [c.id for c in merged.components] == [CREATURE_ID, rigidbody]
        assert merged.components[1].display_name == "Engine"

    def test_components_come_out_ordered_by_type_not_by_id(self):
        """Ordered by TYPE since v3: sorting on a GUID would shuffle the list into an order no
        reader could predict, and the panel draws in this order while the exporter writes in it."""
        document = authoring.read(
            '{"version": 3, "components": ['
            '{"id": "' + CREATURE_ID + '", "type": "Z.Last"},'
            '{"id": "' + component_ids.AGENT + '", "type": "A.First"}]}'
        )
        assert [c.type for c in authoring.merge([document]).components] == ["A.First", "Z.Last"]


class TestFlatten:
    def test_composed_fields_become_slash_paths(self):
        fields, _ = authoring.flatten(creature())
        assert "Box/SizeX" in [f.path for f in fields]

    def test_host_references_are_reported_not_flattened(self):
        fields, hosts = authoring.flatten(creature())
        paths = [f.path for f in fields]
        assert "Shape" not in paths and "Shape/SizeX" not in paths
        assert ("Shape", "shape", False) in [(h.path, h.kind, h.is_list) for h in hosts]
        assert ("Extras", "node", True) in [(h.path, h.kind, h.is_list) for h in hosts]

    def test_defaults_are_read_at_their_schema_type(self):
        by_path = {f.path: f for f in authoring.flatten(creature())[0]}
        assert by_path["Friendly"].default is True
        assert by_path["Lives"].default == 3
        assert by_path["Mode"].default == "Chase"
        assert by_path["Home"].default == [1.0, 2.0, 3.0]
        assert by_path["Facing"].default == [0.0, 0.0, 0.0, 1.0]
        assert by_path["Tint"].default == [1.0, 0.5, 0.0, 1.0]

    def test_an_enum_without_a_declared_default_starts_on_the_first_member(self):
        field = authoring.AuthoredFieldSchema(name="M", type="enum", values=["A", "B"])
        assert authoring.default_of(field) == "A"

    def test_visible_when_survives_flattening(self):
        by_path = {f.path: f for f in authoring.flatten(creature())[0]}
        condition = by_path["FlipChance"].visible_when
        assert condition is not None
        assert condition.field == "Friendly"
        assert condition.equals is True


class TestBuildPayload:
    """The wire rules, pinned against ``AuthoredEntityCore.ValueOf``."""

    def test_every_plain_field_is_written_with_defaults_filling_unset_ones(self):
        payload = authoring.build_payload(creature(), {})
        assert payload["MaxSpeed"] == 7.0
        assert payload["Lives"] == 3
        assert payload["Friendly"] is True
        assert payload["Box"] == {"SizeX": 1.0, "SizeY": 2.0}

    def test_values_at_their_schema_types(self):
        payload = authoring.build_payload(
            creature(),
            {"MaxSpeed": 9.5, "Lives": 1, "Friendly": False, "Home": [4, 5, 6]},
        )
        assert payload["MaxSpeed"] == 9.5
        assert payload["Lives"] == 1
        assert payload["Friendly"] is False
        assert payload["Home"] == [4.0, 5.0, 6.0]

    def test_an_empty_string_with_no_declared_default_is_null(self):
        payload = authoring.build_payload(creature(), {})
        assert payload["Nickname"] is None

    def test_an_empty_string_with_a_declared_default_stays_empty(self):
        payload = authoring.build_payload(creature(), {"Title": ""})
        assert payload["Title"] == ""

    def test_enums_travel_by_member_name(self):
        payload = authoring.build_payload(creature(), {"Mode": "Flee"})
        assert payload["Mode"] == "Flee"

    def test_an_enum_value_outside_the_schema_falls_back_to_the_first_member(self):
        """A typo here would otherwise become a runtime parse error in the game, with this
        entity's name nowhere in the message."""
        payload = authoring.build_payload(creature(), {"Mode": "Zigzag"})
        assert payload["Mode"] == "Idle"

    def test_color_travels_as_an_rgba_object(self):
        payload = authoring.build_payload(creature(), {"Tint": [0.1, 0.2, 0.3, 0.4]})
        assert payload["Tint"] == {"r": 0.1, "g": 0.2, "b": 0.3, "a": 0.4}

    def test_host_references_are_absent_from_the_payload(self):
        """Absent means "unauthored" to the reader -- the truthful description of a bake this
        host does not perform."""
        payload = authoring.build_payload(creature(), {"Shape/SizeX": 5.0})
        assert "Shape" not in payload
        assert "Extras" not in payload

    def test_a_wrong_size_vector_falls_back_rather_than_exporting_garbage(self):
        payload = authoring.build_payload(creature(), {"Home": [1.0], "Facing": "bad"})
        assert payload["Home"] == [0.0, 0.0, 0.0]
        assert payload["Facing"] == [0.0, 0.0, 0.0, 1.0]


class TestCustomInTheDocument:
    def test_components_omit_custom_when_nothing_authored_anything(self):
        """What keeps every scene exported before authored components existed byte-identical:
        the C# side marks Custom JsonIgnore(WhenWritingNull)."""
        assert "Custom" not in schema.EntityComponentsData().to_json()

    def test_custom_serializes_as_id_type_and_opaque_data(self):
        components = schema.EntityComponentsData(
            custom=[schema.AuthoredComponentData(
                id=CREATURE_ID, type="Pingu.Core.Authoring.Creature", data={"MaxSpeed": 7.0})]
        )
        assert components.to_json()["Custom"] == [
            {
                "Id": CREATURE_ID,
                "Type": "Pingu.Core.Authoring.Creature",
                "Data": {"MaxSpeed": 7.0},
            }
        ]

    def test_custom_omits_type_when_there_is_none_rather_than_sending_it_empty(self):
        """Type is optional on the wire. An empty string is not the same as absent to the reader
        that falls back to it, so it must not be written as one."""
        components = schema.EntityComponentsData(
            custom=[schema.AuthoredComponentData(id=CREATURE_ID, data={})]
        )
        assert components.to_json()["Custom"] == [{"Id": CREATURE_ID, "Data": {}}]

    def test_custom_survives_the_writer(self):
        components = schema.EntityComponentsData(
            custom=[schema.AuthoredComponentData(id=CREATURE_ID, data={"Friendly": True})]
        )
        text = writer.dumps(components.to_json())
        assert '"Custom"' in text and '"Friendly": true' in text


class TestLightInTheDocument:
    def test_components_omit_light_when_the_entity_owns_none(self):
        assert "Light" not in schema.EntityComponentsData().to_json()

    def test_an_entity_owned_light_serializes_in_place(self):
        components = schema.EntityComponentsData(
            light=schema.SceneLightData(id="Sun", type="Directional", intensity=1.5)
        )
        emitted = components.to_json()["Light"]
        assert emitted["Id"] == "Sun"
        assert emitted["Type"] == "Directional"
        assert emitted["Intensity"] == 1.5

    def test_light_precedes_custom_matching_the_csharp_declaration_order(self):
        components = schema.EntityComponentsData(
            light=schema.SceneLightData(id="Sun"),
            custom=[schema.AuthoredComponentData(id=CREATURE_ID, data={})],
        )
        keys = list(components.to_json())
        assert keys.index("Light") < keys.index("Custom")


class TestSchemaStamp:
    def test_a_missing_file_stamps_as_zero(self, tmp_path):
        assert authoring.schema_stamp(str(tmp_path / "missing.json")) == (0, 0)

    def test_the_stamp_tracks_content_changes(self, tmp_path):
        path = tmp_path / "authoring-schema.json"
        path.write_text('{"version": 2, "components": []}')
        first = authoring.schema_stamp(str(path))
        path.write_text('{"version": 2, "components": [{"id": "game.new"}]}')
        assert authoring.schema_stamp(str(path)) != first
