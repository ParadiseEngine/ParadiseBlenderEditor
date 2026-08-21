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

    def test_an_engine_component_carries_the_type_the_schema_publishes(self):
        """add_engine looks the CLR name up in the vendored engine schema rather than taking it
        from a table here — the id is the only thing the caller has to know."""
        components = schema.EntityComponentsData()
        components.add_engine(
            component_ids.RENDERABLE, schema.RenderableComponentData(mesh="Models/x.glb"))
        entry = components.to_json()[0]
        assert entry["Id"] == component_ids.RENDERABLE
        assert entry["Type"] == "Paradise.Export.Data.RenderableComponentData"
        assert entry["Data"]["Mesh"] == "Models/x.glb"

    def test_find_returns_the_entry_for_an_id(self):
        components = schema.EntityComponentsData()
        components.add_engine(component_ids.RENDERABLE, schema.RenderableComponentData())
        assert components.find(component_ids.RENDERABLE) is not None
        assert components.find(component_ids.AGENT) is None


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
