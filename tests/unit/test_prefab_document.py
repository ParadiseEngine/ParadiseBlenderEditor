"""Reading and writing ``*.prefab`` documents, on the all-components model.

Mirrors ``Paradise.Assets.Documents.Test/PrefabDocumentTests.cs``. Reading is strict on purpose:
the document is committed source of truth, and a reader that guessed would turn an authoring typo
into a document that loads and is quietly wrong.
"""

from __future__ import annotations

from paradise_assets.document import prefab, well_known

CRATE = "3f2a1b4c-5d6e-4f70-8192-a3b4c5d6e7f8"
LID = "9a8b7c6d-5e4f-4031-8213-4c5d6e7f8091"
RENDERABLE = "bdc4fc87-d7b4-41f1-bc90-fc827005adfc"

META = well_known.META_ID
TRANSFORM = well_known.TRANSFORM_ID

CANONICAL = (
    "schema_version = 1\n"
    "\n[[objects]]\n"
    "\n[[objects.components]]\n"
    f'id = "{META}"\n'
    'type = "meta"\n'
    f'Guid = "{CRATE}"\n'
    'Name = "crate_01"\n'
    "\n[[objects.components]]\n"
    f'id = "{TRANSFORM}"\n'
    'type = "transform"\n'
    "Position = [0.0, 1.5, 0.0]\n"
    "Rotation = [0.0, 0.0, 0.0, 1.0]\n"
    "Scale = [1.0, 1.0, 1.0]\n"
    "\n[[objects.components]]\n"
    f'id = "{RENDERABLE}"\n'
    'type = "Paradise.Export.Data.RenderableComponentData"\n'
    'Mesh = { guid = "11111111-2222-4333-8444-555555555555", path = "Models/crate.glb" }\n'
    "\n[[objects]]\n"
    "\n[[objects.components]]\n"
    f'id = "{META}"\n'
    'type = "meta"\n'
    f'Guid = "{LID}"\n'
    'Name = "lid"\n'
    f'Parent = "{CRATE}"\n'
)


def obj(guid: str, name: str = "x", extra: str = "") -> str:
    """A minimal object: a meta component carrying an identity."""
    return (
        "\n[[objects]]\n\n[[objects.components]]\n"
        f'id = "{META}"\ntype = "meta"\nGuid = "{guid}"\nName = "{name}"\n' + extra
    )


def rejects(text: str) -> str:
    try:
        prefab.loads(text, "x.scene")
    except prefab.PrefabDocumentError as error:
        return str(error)
    raise AssertionError("expected the document to be rejected")


class TestRoundTrip:
    def test_a_canonical_document_round_trips_byte_for_byte(self):
        # THE property: read -> write is the identity on canonical input, or every tool touching
        # a scene would litter diffs with reformatting.
        assert prefab.dumps(prefab.loads(CANONICAL, "x.scene")) == CANONICAL

    def test_identity_name_and_parent_come_from_the_meta_component(self):
        document = prefab.loads(CANONICAL, "x.scene")

        assert len(document.objects) == 2
        assert document.objects[0].guid == CRATE
        assert document.objects[0].name == "crate_01"
        assert document.objects[0].parent is None
        assert document.objects[1].parent == CRATE

    def test_a_payload_sits_flat_beside_id_and_type(self):
        renderable = prefab.loads(CANONICAL, "x.scene").objects[0].component(RENDERABLE)

        assert renderable.type == "Paradise.Export.Data.RenderableComponentData"
        assert "Mesh" in renderable.data
        assert "id" not in renderable.data and "type" not in renderable.data

    def test_an_asset_reference_survives_the_round_trip(self):
        mesh = prefab.loads(CANONICAL, "x.scene").objects[0].component(RENDERABLE).data["Mesh"]

        assert mesh["path"] == "Models/crate.glb"

    def test_a_plain_dict_asset_slot_dumps_as_an_inline_table(self):
        # The overlay records ``{guid, path}`` as a JSON object, which loads back a plain dict.
        # Writing that next to an InlineTable in the same list used to TypeError (mixed array)
        # or, if every row was a dict, emit [[Slots]] headers that drop empty elements.
        document = prefab.loads(CANONICAL, "x.scene")
        document.objects[0].component(RENDERABLE).data["Slots"] = [
            {"guid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "path": "materials/a.toml"},
            {},
        ]

        text = prefab.dumps(document)

        assert "Slots = [" in text
        assert "[[objects.components.Slots]]" not in text
        assert '{ guid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", path = "materials/a.toml" }' in text
        assert "{}" in text
        assert prefab.loads(text, "x.scene").objects[0].component(RENDERABLE).data["Slots"][1] == {}

    def test_an_empty_scene_is_just_its_version(self):
        assert prefab.dumps(prefab.PrefabDocument()) == "schema_version = 1\n"

    def test_component_order_survives(self):
        ids = [c.id for c in prefab.loads(CANONICAL, "x.scene").objects[0].components]

        assert ids == [META, TRANSFORM, RENDERABLE]

    def test_a_removed_marker_round_trips(self):
        text = (
            f"schema_version = 1\n{obj(CRATE)}\n"
            f'[[objects.components]]\nid = "{RENDERABLE}"\nremoved = true\n'
        )

        document = prefab.loads(text, "x.scene")

        assert document.objects[0].component(RENDERABLE).removed is True
        assert prefab.dumps(document) == text

    def test_a_prefab_reference_round_trips(self):
        text = (
            "schema_version = 1\n\n[[objects]]\n"
            f'prefab = {{ guid = "{LID}", path = "prefabs/rail.prefab" }}\n'
            f'\n[[objects.components]]\nid = "{META}"\ntype = "meta"\nGuid = "{CRATE}"\n'
        )

        document = prefab.loads(text, "x.scene")

        assert document.objects[0].prefab.path == "prefabs/rail.prefab"
        assert prefab.dumps(document) == text


class TestStrictness:
    def test_an_object_with_no_identity_is_refused(self):
        text = (
            "schema_version = 1\n\n[[objects]]\n\n[[objects.components]]\n"
            f'id = "{TRANSFORM}"\ntype = "transform"\nPosition = [0.0, 0.0, 0.0]\n'
        )

        assert "meta" in rejects(text)

    def test_a_duplicate_identity_is_refused(self):
        assert "twice" in rejects(f"schema_version = 1\n{obj(CRATE)}{obj(CRATE, 'other')}")

    def test_a_duplicate_component_id_is_refused(self):
        text = f'schema_version = 1\n{obj(CRATE)}\n[[objects.components]]\nid = "{META}"\ntype = "meta"\n'

        assert "twice" in rejects(text)

    def test_a_dangling_parent_is_refused(self):
        assert "does not exist" in rejects(
            "schema_version = 1\n" + obj(CRATE, "a", f'Parent = "{LID}"\n')
        )

    def test_a_parent_cycle_is_refused(self):
        text = (
            "schema_version = 1\n"
            + obj(CRATE, "a", f'Parent = "{LID}"\n')
            + obj(LID, "b", f'Parent = "{CRATE}"\n')
        )

        assert "cycle" in rejects(text)

    def test_an_unknown_document_key_is_refused(self):
        assert "unknown key" in rejects(f"schema_version = 1\nnope = 1\n{obj(CRATE)}")

    def test_an_unknown_object_key_is_refused(self):
        assert "unknown key" in rejects("schema_version = 1\n\n[[objects]]\nnope = 1\n")

    def test_a_component_without_an_id_is_refused(self):
        assert "id" in rejects('schema_version = 1\n\n[[objects]]\n\n[[objects.components]]\ntype = "meta"\n')

    def test_a_removed_component_carrying_fields_is_refused(self):
        text = (
            f"schema_version = 1\n{obj(CRATE)}"
            f'\n[[objects.components]]\nid = "{RENDERABLE}"\nremoved = true\nMesh = "x"\n'
        )

        assert "removed" in rejects(text)

    def test_a_wrong_schema_version_names_the_number(self):
        assert "schema_version = 7" in rejects("schema_version = 7\n")


class TestCarriers:
    def test_a_target_carrier_needs_no_identity_of_its_own(self):
        # A carrier addresses a prefab-local object; the resolved child's guid is always minted,
        # so requiring one here would mean inventing an identity nothing uses.
        text = (
            "schema_version = 1\n\n[[objects]]\n\n[[objects.components]]\n"
            f'id = "{META}"\ntype = "meta"\nParent = "{CRATE}"\nTarget = "{LID}"\n' + obj(CRATE)
        )

        document = prefab.loads(text, "x.scene")

        assert document.objects[0].target == LID
        assert document.objects[0].guid is None


class TestRoots:
    def test_the_single_root_is_inferred_from_the_absence_of_a_parent(self):
        assert prefab.loads(CANONICAL, "x.scene").single_root().guid == CRATE

    def test_a_document_with_two_roots_is_refused(self):
        # There is one kind of document now and every one is instantiable, so "exactly one root" is
        # checked on EVERY read rather than only when something is used as a prefab.
        try:
            prefab.loads(f"schema_version = 1\n{obj(CRATE)}{obj(LID)}", "x.prefab")
        except prefab.PrefabDocumentError as error:
            assert "has 2 root objects" in str(error)
            assert "parent the others beneath it" in str(error)
        else:
            raise AssertionError("expected the document to be refused")

    def test_a_document_with_no_objects_is_refused(self):
        try:
            prefab.loads("schema_version = 1\n", "empty.prefab")
        except prefab.PrefabDocumentError as error:
            assert "has no objects" in str(error)
        else:
            raise AssertionError("expected the document to be refused")


class TestOpaquePayloads:
    def test_an_unrecognised_payload_survives_a_round_trip(self):
        # What makes it safe to open a document full of components this build never heard of.
        text = (
            f"schema_version = 1\n{obj(CRATE)}"
            f'\n[[objects.components]]\nid = "{RENDERABLE}"\ntype = "Nobody.Knows"\n'
            "Count = 3\nRatio = 0.5\nFlag = true\nList = [1, 2]\n"
            '\n[objects.components.Nested]\nInner = "deep"\n'
        )

        assert prefab.dumps(prefab.loads(text, "x.scene")) == text


class TestWellKnownShapes:
    """The shape gate over the two payloads the format itself owns -- mirror of the C# tests."""

    def test_a_malformed_meta_parent_is_refused(self):
        # Before the shape check a non-UUID Parent read as "no parent" -- an object silently
        # promoted to a root is exactly the misread the strict reader exists to prevent.
        text = "schema_version = 1\n" + obj(CRATE) + obj(LID, "lid", 'Parent = "not-a-guid"\n')

        assert "meta.Parent" in rejects(text)

    def test_a_dropped_marker_without_a_target_is_refused(self):
        # Dropping addresses a prefab child; on a plain object it is ignored, and on an instance
        # it deletes the whole subtree -- neither is ever what the author meant.
        text = "schema_version = 1\n" + obj(CRATE, "x", "Dropped = true\n")

        assert "Dropped" in rejects(text)

    def test_a_short_transform_position_is_refused(self):
        text = (
            f"schema_version = 1\n{obj(CRATE)}"
            f'\n[[objects.components]]\nid = "{TRANSFORM}"\ntype = "transform"\nPosition = [0.0, 1.5]\n'
        )

        assert "array of 3 numbers" in rejects(text)

    def test_a_three_element_rotation_is_refused(self):
        text = (
            f"schema_version = 1\n{obj(CRATE)}"
            f'\n[[objects.components]]\nid = "{TRANSFORM}"\ntype = "transform"\nRotation = [0.0, 0.0, 0.0]\n'
        )

        assert "array of 4 numbers" in rejects(text)

    def test_a_misspelled_transform_field_is_refused(self):
        # 'Postion' used to load silently as the origin. transform is a closed set precisely so
        # a typo is an error and not a teleport.
        text = (
            f"schema_version = 1\n{obj(CRATE)}"
            f'\n[[objects.components]]\nid = "{TRANSFORM}"\ntype = "transform"\nPostion = [0.0, 1.5, 0.0]\n'
        )

        assert "Postion" in rejects(text)

    def test_a_game_extended_meta_field_rides_along(self):
        # meta's payload stays open -- only the fields the format defines are shape-checked.
        text = "schema_version = 1\n" + obj(CRATE, "x", 'Zone = "hub"\n')

        assert prefab.dumps(prefab.loads(text, "x.scene")) == text

    def test_a_payload_using_a_reserved_key_is_refused_at_construction(self):
        # The named error RESERVED_KEYS promises: flattened onto the wire, a payload 'id' would
        # collide with the entry's own structure, and dict.update would swallow it silently.
        try:
            prefab.PrefabComponent(RENDERABLE, data={prefab.ID_KEY: "collides"})
        except ValueError as error:
            assert "reserved" in str(error)
        else:
            raise AssertionError("expected the component to be refused")
