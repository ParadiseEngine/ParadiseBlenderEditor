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
                    # An AUTHORABLE list beside the host-referenced one above, so a single
                    # component covers both branches: a list the host bakes stays a reference,
                    # a list of plain rows becomes editable. Shaped like ShiningPie's drop
                    # tables -- records containing their own list, the deepest thing the path
                    # grammar has to carry.
                    {
                        "name": "Tables",
                        "type": "array",
                        "items": {
                            "name": "",
                            "type": "object",
                            "fields": [
                                {"name": "Table", "type": "string", "default": ""},
                                {"name": "MinItems", "type": "int", "default": 0},
                                {
                                    "name": "Entries",
                                    "type": "array",
                                    "items": {
                                        "name": "",
                                        "type": "object",
                                        "fields": [
                                            {"name": "Item", "type": "string", "default": ""},
                                            {"name": "Weight", "type": "int", "default": 1},
                                        ],
                                    },
                                },
                            ],
                        },
                    },
                    # A SCALAR list: a row is one widget, with no container to walk into.
                    {"name": "Tags", "type": "array", "items": {"name": "", "type": "string", "default": ""}},
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

def leafy() -> authoring.AuthoredComponentSchema:
    """A component whose references ARE their values: a mesh, another object, a file.

    Separate from :func:`creature` rather than another field on it, because the two families read
    differently — one fills declared leaves, the other writes at its own path — and a fixture that
    mixed them would let a test pass while proving the wrong half.
    """
    document = json.dumps(
        {
            "version": 3,
            "components": [
                {
                    "id": "b0000000-0000-4000-8000-00000000000a",
                    "type": "Game.Leafy",
                    "displayName": "Leafy",
                    "fields": [
                        {"name": "Mesh", "type": "string", "authoredBy": "mesh"},
                        {"name": "Target", "type": "string", "authoredBy": "entity"},
                        {"name": "Sprite", "type": "string", "authoredBy": "sprite"},
                        {
                            "name": "Sheet",
                            "type": "string",
                            "authoredBy": "asset",
                            "assetKinds": [".ktx2", ".png"],
                        },
                    ],
                }
            ],
        }
    )
    return authoring.read(document).components[0]


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

    def test_a_reference_this_host_cannot_bake_is_absent_from_the_payload(self):
        """Absent means "unauthored" to the reader -- the truthful description of a bake this
        host does not perform. Extras is a MESH reference, which is still one of those."""
        payload = authoring.build_payload(creature(), {"Extras/Whatever": 5.0})
        assert "Extras" not in payload

    def test_a_shape_reference_contributes_its_declared_leaves(self):
        """A shape is authorable here now: the exporter bakes a whole ColliderShapeData and writes
        back whichever names the record declared, so the record says which parts of a shape it
        means. A leaf the bake did not fill takes the record's own default, which is what an
        unassigned object slot means -- and leaves the runtime free to refuse it."""
        payload = authoring.build_payload(creature(), {"Shape/SizeX": 5.0})
        assert payload["Shape"]["SizeX"] == 5.0

    def test_a_wrong_size_vector_falls_back_rather_than_exporting_garbage(self):
        payload = authoring.build_payload(creature(), {"Home": [1.0], "Facing": "bad"})
        assert payload["Home"] == [0.0, 0.0, 0.0]
        assert payload["Facing"] == [0.0, 0.0, 0.0, 1.0]


class TestComponentsInTheDocument:
    def test_an_entity_authoring_nothing_has_an_empty_list(self):
        """This used to assert the word "Custom" was absent — the trick that kept scenes exported
        before authored components existed byte-identical. There is one list now, so absence is
        an empty array rather than a missing key."""
        assert schema.EntityComponentsData().to_json() == []

    def test_a_component_serializes_as_id_type_and_opaque_data(self):
        components = schema.EntityComponentsData()
        components.add(schema.AuthoredComponentData(
            id=CREATURE_ID, type="Pingu.Core.Authoring.Creature", data={"MaxSpeed": 7.0}))
        assert components.to_json() == [
            {
                "Id": CREATURE_ID,
                "Type": "Pingu.Core.Authoring.Creature",
                "Data": {"MaxSpeed": 7.0},
            }
        ]

    def test_type_is_omitted_when_there_is_none_rather_than_sent_empty(self):
        """Type is optional on the wire. An empty string is not the same as absent to the reader
        that falls back to it, so it must not be written as one."""
        components = schema.EntityComponentsData()
        components.add(schema.AuthoredComponentData(id=CREATURE_ID, data={}))
        assert components.to_json() == [{"Id": CREATURE_ID, "Data": {}}]

    def test_components_survive_the_writer(self):
        components = schema.EntityComponentsData()
        components.add(schema.AuthoredComponentData(id=CREATURE_ID, data={"Friendly": True}))
        text = writer.dumps(components.to_json())
        assert CREATURE_ID in text and '"Friendly": true' in text

    def test_an_engine_component_carries_the_type_the_schema_publishes(self, tmp_path):
        """add_engine looks the CLR name up in the GAME's dumped schema — which describes the
        engine's components too, because the launcher that dumps it scans its references. The id
        is the only thing the caller has to know."""
        data_dir = _data_dir_with_engine_schema(tmp_path)
        components = schema.EntityComponentsData(data_dir=data_dir)
        components.add_engine(
            component_ids.RENDERABLE, schema.RenderableComponentData(mesh="Models/x.glb"))
        entry = components.to_json()[0]
        assert entry["Id"] == component_ids.RENDERABLE
        assert entry["Type"] == "Paradise.Export.Data.RenderableComponentData"
        assert entry["Data"]["Mesh"] == "Models/x.glb"

    def test_find_returns_the_entry_for_an_id(self, tmp_path):
        data_dir = _data_dir_with_engine_schema(tmp_path)
        components = schema.EntityComponentsData(data_dir=data_dir)
        components.add_engine(component_ids.RENDERABLE, schema.RenderableComponentData())
        assert components.find(component_ids.RENDERABLE) is not None
        assert components.find(component_ids.AGENT) is None

    def test_an_engine_component_without_a_schema_refuses_rather_than_omitting_the_type(
            self, tmp_path):
        """The determinism rule. The engine reads Type only when the id fails to resolve, so
        dropping it would still export — and the same .blend would then produce two different
        data/scenes/*.json depending on whether the game had been built, one of which gets
        committed. An export is reproducible or it is not."""
        components = schema.EntityComponentsData(data_dir=str(tmp_path))
        with pytest.raises(KeyError, match="authoring schema"):
            components.add_engine(component_ids.RENDERABLE, schema.RenderableComponentData())

    def test_engine_components_need_a_data_dir_at_all(self):
        components = schema.EntityComponentsData()
        with pytest.raises(ValueError, match="data_dir"):
            components.add_engine(component_ids.RENDERABLE, schema.RenderableComponentData())



def _data_dir_with_engine_schema(tmp_path) -> str:
    """A data directory holding the kind of document a LAUNCHER dumps: the engine's components
    merged in alongside the game's, which is what makes this host able to name them at all."""
    import json

    (tmp_path / "authoring-schema.json").write_text(json.dumps({
        "version": 3,
        "components": [
            {
                "id": component_ids.RENDERABLE,
                "type": "Paradise.Export.Data.RenderableComponentData",
                "displayName": "Renderable",
                "fields": [],
            },
        ],
    }), encoding="utf-8")
    authoring._cache.clear()
    return str(tmp_path)

class TestSchemaStamp:
    def test_a_missing_file_stamps_as_zero(self, tmp_path):
        assert authoring.schema_stamp(str(tmp_path / "missing.json")) == (0, 0)

    def test_the_stamp_tracks_content_changes(self, tmp_path):
        path = tmp_path / "authoring-schema.json"
        path.write_text('{"version": 2, "components": []}')
        first = authoring.schema_stamp(str(path))
        path.write_text('{"version": 2, "components": [{"id": "game.new"}]}')
        assert authoring.schema_stamp(str(path)) != first


# --------------------------------------------------------------------------------------
# Authored lists
# --------------------------------------------------------------------------------------

#: A payload for the creature's `Tables`, with RAGGED inner counts on purpose: table 1 holds no
#: entries at all, which is the case that catches a builder that seeds arrays from the schema
#: rather than from the data.
TABLES_PAYLOAD = [
    {"Table": "Rubble", "MinItems": 0, "Entries": [{"Item": "metal", "Weight": 5}]},
    {"Table": "Crate", "MinItems": 1, "Entries": []},
    {
        "Table": "Chest",
        "MinItems": 2,
        "Entries": [{"Item": "gold", "Weight": 1}, {"Item": "gem", "Weight": 9}],
    },
]


class TestOutlineArrays:
    def test_a_list_is_declared_even_with_no_rows(self):
        # Neither a field nor a host ref: the panel still has to draw its header and Add button,
        # which is the whole reason `arrays` is a third output rather than folded into one.
        plan = authoring.outline(creature())
        assert [f.path for f in plan.fields if f.path.startswith("Tables")] == []
        assert ("Tables", 0) in [(a.path, a.count) for a in plan.arrays]

    def test_rows_expand_to_indexed_paths_in_order(self):
        plan = authoring.outline(creature(), {"Tables": 2})
        assert [f.path for f in plan.fields if f.path.startswith("Tables")] == [
            "Tables/0/Table", "Tables/0/MinItems", "Tables/1/Table", "Tables/1/MinItems",
        ]

    def test_a_nested_list_expands_per_row_not_per_schema(self):
        # The property no schema path can express: two rows of the same declaration holding
        # different numbers of entries.
        plan = authoring.outline(
            creature(), {"Tables": 2, "Tables/0/Entries": 3, "Tables/1/Entries": 0})
        paths = [f.path for f in plan.fields]
        assert "Tables/0/Entries/2/Weight" in paths
        assert not any(p.startswith("Tables/1/Entries/") for p in paths)

    def test_an_authored_by_list_stays_a_host_reference(self):
        # Even when counts name it -- a collider's shapes are baked from the objects the entity
        # points at, and a row editor over them would be a second, lying copy of that list.
        plan = authoring.outline(creature(), {"Extras": 4})
        assert ("Extras", "node", True) in [(h.path, h.kind, h.is_list) for h in plan.hosts]
        assert [a for a in plan.arrays if a.path == "Extras"] == []
        assert not any(f.path.startswith("Extras") for f in plan.fields)

    def test_a_scalar_list_yields_one_leaf_per_row(self):
        plan = authoring.outline(creature(), {"Tags": 2})
        tags = [f for f in plan.fields if f.path.startswith("Tags")]
        assert [f.path for f in tags] == ["Tags/0", "Tags/1"]
        assert tags[0].type == authoring.TYPE_STRING
        assert [a.rows_are_records for a in plan.arrays if a.path == "Tags"] == [False]

    def test_arrays_come_out_parent_before_child(self):
        # build_payload seeds in this order and depends on it: a nested list can only be created
        # once the row holding it exists.
        plan = authoring.outline(creature(), {"Tables": 2, "Tables/0/Entries": 1})
        paths = [a.path for a in plan.arrays]
        assert paths.index("Tables") < paths.index("Tables/0/Entries")
        assert paths.index("Tables/0/Entries") < paths.index("Tables/1/Entries")

    def test_a_row_is_titled_by_its_first_string_leaf(self):
        plan = authoring.outline(creature(), {"Tables": 1})
        assert [a.row_title_path for a in plan.arrays if a.path == "Tables"] == ["Table"]
        assert [a.row_title_path for a in plan.arrays if a.path == "Tags"] == [None]

    def test_a_count_is_clamped_rather_than_trusted(self):
        # The count reaches here from a hand-editable store; a draw() looping a billion times
        # hangs Blender with no way back to the button that would fix it.
        plan = authoring.outline(creature(), {"Tags": 10 ** 9})
        assert [a.count for a in plan.arrays if a.path == "Tags"] == [authoring.MAX_ROWS]

    def test_a_nonsense_count_reads_as_empty_rather_than_raising(self):
        plan = authoring.outline(creature(), {"Tags": "three", "Tables": -2})
        assert [a.count for a in plan.arrays if a.path in ("Tags", "Tables")] == [0, 0]

    def test_flatten_still_returns_two_lists(self):
        fields, hosts = authoring.flatten(creature(), {"Tables": 1})
        assert "Tables/0/Table" in [f.path for f in fields]
        assert "Extras" in [h.path for h in hosts]


class TestCountsOf:
    def test_counts_are_per_instance_not_per_declaration(self):
        counts = authoring.counts_of(creature(), {"Tables": TABLES_PAYLOAD})
        assert counts["Tables"] == 3
        assert counts["Tables/0/Entries"] == 1
        assert counts["Tables/1/Entries"] == 0
        assert counts["Tables/2/Entries"] == 2

    def test_an_absent_member_counts_as_zero(self):
        assert authoring.counts_of(creature(), {})["Tables"] == 0

    def test_a_member_that_is_not_a_list_counts_as_zero(self):
        # The panel's job is to show the author what is there and let them fix it, not to refuse
        # to draw over one malformed key.
        assert authoring.counts_of(creature(), {"Tables": "nope"})["Tables"] == 0

    def test_a_host_referenced_list_is_absent_from_the_mapping(self):
        assert "Extras" not in authoring.counts_of(creature(), {"Extras": [1, 2]})

    def test_counts_are_the_inverse_of_outline(self):
        counts = authoring.counts_of(creature(), {"Tables": TABLES_PAYLOAD})
        plan = authoring.outline(creature(), counts)
        assert "Tables/2/Entries/1/Weight" in [f.path for f in plan.fields]


class TestValueAt:
    def test_it_follows_list_indices_and_object_members(self):
        payload = {"Tables": TABLES_PAYLOAD}
        assert authoring.value_at(payload, "Tables/2/Entries/1/Item") == "gem"
        assert authoring.value_at(payload, "Tables/0/Table") == "Rubble"

    def test_an_out_of_range_index_falls_back(self):
        payload = {"Tables": TABLES_PAYLOAD}
        assert authoring.value_at(payload, "Tables/9/Table", "fb") == "fb"
        assert authoring.value_at(payload, "Tables/1/Entries/0/Item", "fb") == "fb"

    def test_a_digit_against_an_object_reads_the_member_named_that(self):
        # The CONTAINER decides how a segment is read, not the segment's spelling.
        assert authoring.value_at({"0": "zero"}, "0") == "zero"

    def test_an_explicit_null_falls_back(self):
        assert authoring.value_at({"Title": None}, "Title", "fb") == "fb"


class TestBuildPayloadArrays:
    def _values(self, component, payload, counts):
        return {
            f.path: authoring.value_at(payload, f.path, f.default)
            for f in authoring.outline(component, counts).fields
        }

    def test_rows_come_back_as_a_json_list_in_index_order(self):
        component = creature()
        payload = {"Tables": TABLES_PAYLOAD}
        counts = authoring.counts_of(component, payload)
        built = authoring.build_payload(component, self._values(component, payload, counts), counts)
        assert [t["Table"] for t in built["Tables"]] == ["Rubble", "Crate", "Chest"]
        assert built["Tables"][2]["Entries"][1] == {"Item": "gem", "Weight": 9}

    def test_a_list_authored_with_no_rows_is_written_as_empty_not_omitted(self):
        # The member IS authored, and it is authored empty. Omitting it would read to the engine
        # as unauthored and silently restore the record's own initializer.
        built = authoring.build_payload(creature(), {}, {"Tables": 0})
        assert built["Tables"] == []

    def test_counts_none_leaves_arrays_absent(self):
        # The entity-export guarantee: a caller holding no list data at all must keep producing
        # exactly the bytes it always has.
        assert "Tables" not in authoring.build_payload(creature(), {})
        assert "Tags" not in authoring.build_payload(creature(), {})

    def test_a_hole_yields_an_empty_row_never_a_null(self):
        # A null row is something the engine's generated reader would dereference; an empty
        # object is what it fills from the record's own initializers.
        built = authoring.build_payload(creature(), {"Tables/1/Table": "Only"}, {"Tables": 2})
        assert built["Tables"][0] is not None and built["Tables"][1]["Table"] == "Only"

    def test_keys_come_out_in_schema_order(self):
        """A save with no edits must not reshuffle the file.

        Seeding every list before every leaf would hoist each row's nested list above its
        siblings -- semantically identical, and a whole-file diff on a document that is hand
        edited and read in review.
        """
        built = authoring.build_payload(
            creature(), {}, {"Tables": 1, "Tables/0/Entries": 1})
        assert list(built["Tables"][0]) == ["Table", "MinItems", "Entries"]

    def test_rows_are_distinct_objects(self):
        # A shared {} appended twice makes two rows the same object -- a bug that survives every
        # test written against a single row.
        built = authoring.build_payload(creature(), {}, {"Tables": 2})
        built["Tables"][0]["Table"] = "changed"
        assert built["Tables"][1]["Table"] != "changed"

    @pytest.mark.parametrize(
        "tables",
        [
            [],
            [{"Table": "One", "MinItems": 0, "Entries": []}],
            TABLES_PAYLOAD,
            [{"Table": "Deep", "MinItems": 9, "Entries": [{"Item": "i", "Weight": 2}] * 3}],
        ],
        ids=["empty", "single", "ragged", "nested"],
    )
    def test_load_edit_save_is_lossless_for_rows(self, tables):
        """The property the whole feature rests on.

        Loading reads leaves out of the payload at the paths `outline` names; saving writes them
        back at the same paths. If those two ever disagree about anything -- an index, a container
        kind, an empty list -- this equality fails, and it fails on the exact shape that broke.

        The fixture must be a COMPLETE payload for the equality to hold: build_payload fills
        every unset member with its default, so a partial fixture would fail for an uninteresting
        reason.
        """
        component = creature()
        payload = {"Tables": tables, "Tags": ["a", "b"]}
        counts = authoring.counts_of(component, payload)
        rebuilt = authoring.build_payload(
            component, self._values(component, payload, counts), counts)
        assert {"Tables": rebuilt["Tables"], "Tags": rebuilt["Tags"]} == payload


class TestRowPaths:
    """The renumbering algebra: pure strings, and the riskiest logic in list editing."""

    def test_removal_shifts_every_higher_row_down(self):
        mapping = authoring.removal_mapping(4, 1)
        assert authoring.renumber("Tables/2/Table", "Tables", mapping) == "Tables/1/Table"
        assert authoring.renumber("Tables/3/Table", "Tables", mapping) == "Tables/2/Table"

    def test_a_removed_row_takes_its_whole_subtree_with_it(self):
        mapping = authoring.removal_mapping(4, 1)
        for path in ("Tables/1/Table", "Tables/1/Entries#", "Tables/1/Entries/0/Item"):
            assert authoring.renumber(path, "Tables", mapping) is None

    def test_a_nested_count_key_moves_with_its_row(self):
        # Without this the moved row's entries would be present but reported as zero, and the
        # next save would drop them.
        mapping = authoring.removal_mapping(4, 1)
        assert authoring.renumber("Tables/2/Entries#", "Tables", mapping) == "Tables/1/Entries#"
        assert (authoring.renumber("Tables/2/Entries/1/Weight", "Tables", mapping)
                == "Tables/1/Entries/1/Weight")

    def test_a_sibling_sharing_the_prefix_is_untouched(self):
        mapping = authoring.removal_mapping(4, 1)
        assert authoring.row_index_of("TablesEnabled", "Tables") is None
        assert authoring.renumber("TablesEnabled", "Tables", mapping) == "TablesEnabled"
        assert authoring.renumber("TablesCount/0", "Tables", mapping) == "TablesCount/0"

    def test_the_arrays_own_count_key_is_not_one_of_its_rows(self):
        assert authoring.row_index_of("Tables#", "Tables") is None

    def test_an_index_is_a_whole_segment(self):
        assert authoring.row_index_of("Tables/10/X", "Tables") == 10

    def test_swap_exchanges_two_rows_and_leaves_the_rest(self):
        mapping = authoring.swap_mapping(3, 0, 1)
        assert authoring.renumber("Tables/0/Table", "Tables", mapping) == "Tables/1/Table"
        assert authoring.renumber("Tables/1/Table", "Tables", mapping) == "Tables/0/Table"
        assert authoring.renumber("Tables/2/Table", "Tables", mapping) == "Tables/2/Table"

    def test_row_container_is_the_nearest_enclosing_row(self):
        assert authoring.row_container_of("Tables/0/Entries/1/Weight") == "Tables/0/Entries/1"
        assert authoring.row_container_of("Tables/0/Table") == "Tables/0"
        assert authoring.row_container_of("Box/SizeX") == ""

    def test_relative_to_strips_the_container(self):
        assert authoring.relative_to("Tables/0/Entries/1/Weight", "Tables/0/Entries/1") == "Weight"
        assert authoring.relative_to("Box/SizeX", "") == "Box/SizeX"


#: A component authored the way ShiningPie's trigger volumes are: a pose reference beside a plain
#: field. The reference declares only PART of the pose, plus a leaf no exporter knows how to bake,
#: because both are the interesting cases — a record takes what it means and an exporter fills only
#: what it recognises.
TRIGGER_LIKE = json.dumps(
    {
        "version": 3,
        "components": [
            {
                "id": "b6c7e010-577b-475c-ae94-7951b00f8558",
                "type": "ShiningPie.Authoring.CameraTriggerMarker",
                "displayName": "Camera trigger",
                "fields": [
                    {
                        "name": "Framing",
                        "type": "object",
                        "authoredBy": "transform",
                        "fields": [
                            {"name": "Position", "type": "vector3"},
                            {"name": "Yaw", "type": "float", "unit": "radians"},
                            {"name": "Label", "type": "string", "default": ""},
                        ],
                    },
                    {"name": "Fov", "type": "float", "default": 50},
                ],
            }
        ],
    }
)


def camera_trigger() -> authoring.AuthoredComponentSchema:
    return authoring.read(TRIGGER_LIKE).components[0]


def hosted() -> authoring.AuthoredComponentSchema:
    """Every remaining host kind: self, light, camera, as a game component would declare them."""
    document = json.dumps(
        {
            "version": 3,
            "components": [
                {
                    "id": "b0000000-0000-4000-8000-00000000000b",
                    "type": "Game.Hosted",
                    "displayName": "Hosted",
                    "fields": [
                        {"name": "Ident", "type": "string", "authoredBy": "id"},
                        {"name": "Label", "type": "string", "authoredBy": "name"},
                        {"name": "Parent", "type": "string", "authoredBy": "parent"},
                        {"name": "At", "type": "vector3", "authoredBy": "local-position"},
                        {
                            "name": "Lamp",
                            "type": "object",
                            "authoredBy": "light",
                            "fields": [
                                {"name": "Type", "type": "enum", "values": ["Directional", "Point", "Spot"]},
                                {"name": "Intensity", "type": "float", "default": 1},
                                {"name": "Color", "type": "color"},
                            ],
                        },
                        {
                            "name": "Eye",
                            "type": "object",
                            "authoredBy": "camera",
                            "fields": [
                                {"name": "Fov", "type": "float", "default": 50},
                                {"name": "Near", "type": "float", "default": 0.1},
                            ],
                        },
                    ],
                }
            ],
        }
    )
    return authoring.read(document).components[0]


class TestTransformReferences:
    """Host-object references this host authors rather than merely reports."""

    def test_a_pose_reference_is_a_host_ref_not_a_set_of_leaves(self):
        # Same rule every host reference follows: the leaves are what the EXPORTER writes, so
        # they must not also become editable fields — two ways to set one value is two values.
        fields, hosts = authoring.flatten(camera_trigger())

        assert [f.path for f in fields] == ["Fov"]
        assert [(h.path, h.kind) for h in hosts] == [("Framing", authoring.HOST_TRANSFORM)]

    def test_a_pose_reference_is_authorable_here_and_names_what_it_bakes(self):
        # `bakes` IS the contract between the picker and the exporter: it says which parts of the
        # pose this record asked for. Label is declared and is not a pose leaf, so it is left out
        # rather than filled with something invented.
        host = authoring.flatten(camera_trigger())[1][0]

        assert host.is_authorable
        assert host.bakes == ("Position", "Yaw")

    def test_a_shape_reference_is_authorable_and_names_what_it_bakes(self):
        # The second kind this host implements. Same picker as a pose; what differs is only what
        # the exporter takes off the object you point at — the collider drawn ON it, rather than
        # where it stands. Every leaf the record declared travels, because the bake produces a
        # whole shape and the record chooses which parts of it it means.
        hosts = {h.path: h for h in authoring.flatten(creature())[1]}

        assert hosts["Shape"].is_authorable
        assert "SizeX" in hosts["Shape"].bakes

    def test_a_list_of_references_stays_unauthorable_whatever_its_kind(self):
        # `Extras` is an authoredBy:"node" ARRAY, and BOTH halves of that matter. "node" is a kind
        # this host does not implement at all — but the reason this is unauthorable is that it is a
        # LIST, which is the is_list short-circuit and holds for every kind including those
        # this host now bakes. Said explicitly because the test previously claimed Extras was a
        # mesh reference: it is not, and mesh is a kind this host DOES bake, so the old wording
        # would have kept passing if the leaf kinds regressed.
        hosts = {h.path: h for h in authoring.flatten(creature())[1]}

        assert hosts["Extras"].kind == "node"
        assert hosts["Extras"].is_list
        assert not hosts["Extras"].is_authorable

    def test_the_leaf_kinds_are_authorable_and_carry_their_type(self):
        # mesh / entity / sprite / asset differ from a pose or a shape in SHAPE, not in picker:
        # the reference IS the value, so there are no leaves to fill and the bake writes at the
        # reference's own path. `leaf_type` is what tells the two families apart.
        hosts = {h.path: h for h in authoring.flatten(leafy())[1]}

        for path in ("Mesh", "Target", "Sprite", "Sheet"):
            assert hosts[path].is_authorable, path
            assert hosts[path].leaf_type == "string", path
            assert hosts[path].bakes == (), path
            assert hosts[path].stores_slot, path

    def test_self_kinds_are_authorable_and_store_no_slot(self):
        hosts = {h.path: h for h in authoring.flatten(hosted())[1]}

        for path, kind in (
            ("Ident", authoring.HOST_ID),
            ("Label", authoring.HOST_NAME),
            ("Parent", authoring.HOST_PARENT),
            ("At", authoring.HOST_LOCAL_POSITION),
        ):
            assert hosts[path].kind == kind
            assert hosts[path].is_authorable
            assert not hosts[path].stores_slot

    def test_a_light_reference_is_authorable_and_names_what_it_bakes(self):
        hosts = {h.path: h for h in authoring.flatten(hosted())[1]}

        assert hosts["Lamp"].is_authorable
        assert hosts["Lamp"].stores_slot
        assert hosts["Lamp"].bakes == ("Type", "Intensity", "Color")

    def test_a_camera_reference_is_authorable_and_names_what_it_bakes(self):
        hosts = {h.path: h for h in authoring.flatten(hosted())[1]}

        assert hosts["Eye"].is_authorable
        assert hosts["Eye"].bakes == ("Fov", "Near")

    def test_an_unassigned_light_or_camera_still_carries_declared_leaves(self):
        payload = authoring.build_payload(hosted(), {})

        assert payload["Lamp"]["Intensity"] == 1
        assert payload["Eye"]["Fov"] == 50
        assert payload["Ident"] is None

    def test_an_asset_reference_carries_the_extensions_it_accepts(self):
        # The picker is a file browser, and what it filters on comes off the field rather than
        # from anything the panel knows.
        hosts = {h.path: h for h in authoring.flatten(leafy())[1]}

        assert hosts["Sheet"].asset_kinds == (".ktx2", ".png")

    def test_a_leaf_reference_is_written_at_its_own_path(self):
        # Where a pose writes Destination/Position, a leaf writes Mesh. Asserted through
        # build_payload because that is the seam the exporter actually uses.
        payload = authoring.build_payload(leafy(), {"Mesh": "Models/crate.glb", "Target": "Player"})

        assert payload["Mesh"] == "Models/crate.glb"
        assert payload["Target"] == "Player"

    def test_an_unassigned_leaf_reference_exports_empty_rather_than_absent(self):
        # Absent would read as "this host does not implement the kind"; present-and-empty reads as
        # "nobody picked one", and only the second is something a loader can refuse by name.
        payload = authoring.build_payload(leafy(), {})

        assert "Mesh" in payload
        assert payload["Mesh"] is None

    def test_a_list_of_pose_references_is_not_authorable(self):
        # A row editor over a pointer list would be a second, lying copy of the list itself — the
        # same reason a collider list stays a reference. Guarded because `bakes` alone would
        # otherwise make one look authorable.
        document = json.dumps(
            {
                "version": 3,
                "components": [
                    {
                        "id": "b6c7e010-577b-475c-ae94-7951b00f8559",
                        "type": "Game.Waypoints",
                        "fields": [
                            {
                                "name": "Points",
                                "type": "array",
                                "items": {
                                    "name": "",
                                    "type": "object",
                                    "authoredBy": "transform",
                                    "fields": [{"name": "Position", "type": "vector3"}],
                                },
                            }
                        ],
                    }
                ],
            }
        )
        host = authoring.flatten(
            authoring.read(document).components[0])[1][0]

        assert host.is_list
        assert not host.is_authorable

    def test_baked_leaves_re_nest_into_the_payload(self):
        # What the exporter produces has to land under the reference's own object, exactly as a
        # composed field would — the runtime deserializes one record either way and cannot tell
        # that half of it came from an object slot.
        payload = authoring.build_payload(
            camera_trigger(),
            {"Framing/Position": [1.0, 2.0, 3.0], "Framing/Yaw": 0.5, "Fov": 40.0},
        )

        assert payload["Framing"]["Position"] == [1.0, 2.0, 3.0]
        assert payload["Framing"]["Yaw"] == 0.5
        assert payload["Fov"] == 40.0

    def test_an_unassigned_reference_leaves_the_records_own_defaults(self):
        # An empty object slot bakes nothing, so the payload carries what the record declares.
        # The RUNTIME decides whether that is acceptable — ShiningPie refuses an unset destination,
        # because the world origin is a real place — and it can only do that if the export is
        # honest about the field being unauthored rather than inventing a pose here.
        payload = authoring.build_payload(camera_trigger(), {"Fov": 40.0})

        assert payload["Framing"]["Position"] == [0.0, 0.0, 0.0]
        assert payload["Framing"]["Yaw"] == 0.0

    def test_a_reference_nested_in_a_composed_field_still_bakes(self):
        # The regression. `path` is built by the same `prefix + name` recursion every composed
        # field uses, so a reference inside one gets a MULTI-SEGMENT path. Re-deriving its leaves
        # afterwards by matching that path against the component's top-level field names found
        # nothing, and build_payload dropped the whole reference — baked pose and defaults alike,
        # silently, which is worse than the unassigned case this design goes out of its way to
        # make refusable. The engine permits the nesting (its generator reads `authoredBy` at
        # every depth), so this was live rather than latent.
        document = json.dumps(
            {
                "version": 3,
                "components": [
                    {
                        "id": "b6c7e010-577b-475c-ae94-7951b00f855a",
                        "type": "Game.Nested",
                        "fields": [
                            {
                                "name": "Container",
                                "type": "object",
                                "fields": [
                                    {
                                        "name": "Destination",
                                        "type": "object",
                                        "authoredBy": "transform",
                                        "fields": [{"name": "Position", "type": "vector3"}],
                                    },
                                    {"name": "Radius", "type": "float", "default": 2},
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        component = authoring.read(document).components[0]

        host = authoring.flatten(component)[1][0]
        assert host.path == "Container/Destination"
        assert host.is_authorable and host.bakes == ("Position",)

        payload = authoring.build_payload(
            component, {"Container/Destination/Position": [1.0, 2.0, 3.0]}
        )
        assert payload["Container"]["Destination"]["Position"] == [1.0, 2.0, 3.0]
        assert payload["Container"]["Radius"] == 2.0

    def test_a_nested_reference_nobody_assigned_still_carries_its_defaults(self):
        # The other half: the dropped-entirely bug also robbed the runtime of the values its
        # "refuse an unset destination" check reads. An unassigned nested slot must reach the wire
        # at its schema defaults, exactly as a top-level one does.
        document = json.dumps(
            {
                "version": 3,
                "components": [
                    {
                        "id": "b6c7e010-577b-475c-ae94-7951b00f855b",
                        "type": "Game.Nested",
                        "fields": [
                            {
                                "name": "Container",
                                "type": "object",
                                "fields": [
                                    {
                                        "name": "Destination",
                                        "type": "object",
                                        "authoredBy": "transform",
                                        "fields": [
                                            {"name": "Position", "type": "vector3"},
                                            {"name": "Scale", "type": "vector3"},
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        payload = authoring.build_payload(authoring.read(document).components[0], {})

        assert payload["Container"]["Destination"]["Position"] == [0.0, 0.0, 0.0]
        assert payload["Container"]["Destination"]["Scale"] == [0.0, 0.0, 0.0]


class TestEngineIdDrift:
    """``component_ids.check_engine_ids`` — what replaced the unit test that asserted every
    constant appeared in a vendored copy of the engine's schema.

    It checks the same thing against a better subject: the schema the game's launcher dumped, so
    the constants are compared with the engine that game is actually built against rather than
    with a checked-in copy of some engine.
    """

    @staticmethod
    def _write(tmp_path, ids: list[str]) -> str:
        import json

        (tmp_path / "authoring-schema.json").write_text(json.dumps({
            "version": 3,
            "components": [
                {"id": i, "type": f"Engine.C{n}", "displayName": "C", "fields": []}
                for n, i in enumerate(ids)
            ],
        }), encoding="utf-8")
        authoring._cache.clear()
        return str(tmp_path)

    def test_a_schema_carrying_every_constant_reports_nothing(self, tmp_path):
        named = [
            value for name, value in vars(component_ids).items()
            if name.isupper() and isinstance(value, str)
        ]
        assert named, "no constants found — the introspection above stopped matching"
        assert component_ids.check_engine_ids(self._write(tmp_path, named)) == []

    def test_a_constant_the_schema_does_not_know_is_named(self, tmp_path):
        """The drift this exists for: the engine renames or drops a component and the transcribed
        constant is left pointing at nothing."""
        named = [
            value for name, value in vars(component_ids).items()
            if name.isupper() and isinstance(value, str)
        ]
        reported = component_ids.check_engine_ids(self._write(tmp_path, named[1:]))
        assert len(reported) == 1
        assert named[0] in reported[0]

    def test_no_schema_at_all_reports_nothing(self, tmp_path):
        """Silence, not a wall of drift. An unbuilt project is already explained once by
        schema_load_error; repeating it per constant would bury it."""
        authoring._cache.clear()
        assert component_ids.check_engine_ids(str(tmp_path)) == []
