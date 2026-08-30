"""Tests for reading and writing ``*.scene`` documents.

Reading is strict on purpose: the document is committed source of truth, and a reader that
guessed would turn an authoring typo into a scene that loads and is quietly wrong. Each rejection
below is one the C# reader also makes.
"""

from __future__ import annotations

from paradise_assets.document import scene

GUID = "11111111-2222-4333-8444-555555555555"
OTHER = "99999999-8888-4777-8666-555555555555"

CANONICAL = (
    "schema_version = 1\n"
    "\n[[objects]]\n"
    f'guid = "{GUID}"\n'
    'name = "crate"\n'
    "\n[objects.transform]\n"
    "position = [0.0, 1.5, 0.0]\n"
    "rotation = [0.0, 0.0, 0.0, 1.0]\n"
    "scale = [1.0, 1.0, 1.0]\n"
    "\n[[objects.components]]\n"
    f'id = "{OTHER}"\n'
    'type = "Paradise.Export.Data.RenderableComponentData"\n'
    "\n[objects.components.data]\n"
    'Mesh = "Models/crate.glb"\n'
)


def rejects(text: str) -> str:
    try:
        scene.loads(text, "x.scene")
    except scene.SceneDocumentError as error:
        return str(error)
    raise AssertionError("expected the document to be rejected")


class TestRoundTrip:
    def test_a_canonical_document_round_trips_byte_for_byte(self):
        # THE property: read -> write must be the identity on canonical input, or every tool
        # touching a scene would litter diffs with reformatting.
        assert scene.dumps(scene.loads(CANONICAL, "x.scene")) == CANONICAL

    def test_the_model_reflects_the_document(self):
        document = scene.loads(CANONICAL, "x.scene")
        assert len(document.objects) == 1
        crate = document.objects[0]
        assert crate.name == "crate"
        assert crate.parent is None
        assert crate.transform.position == (0.0, 1.5, 0.0)
        assert crate.components[0].data == {"Mesh": "Models/crate.glb"}

    def test_an_empty_scene_is_just_its_version(self):
        assert scene.dumps(scene.SceneDocument()) == "schema_version = 1\n"

    def test_an_identity_transform_is_omitted(self):
        # The common case for a freshly minted object stays one line in a diff.
        document = scene.SceneDocument()
        document.objects.append(scene.SceneObject(guid=GUID, name="x"))
        assert "transform" not in scene.dumps(document)

    def test_component_order_survives(self):
        # Order is data: the runtime applies components in document order.
        third = "77777777-6666-4555-8444-333333333333"
        document = scene.SceneDocument()
        obj = scene.SceneObject(guid=GUID, name="x")
        obj.components = [scene.SceneComponent(id=OTHER), scene.SceneComponent(id=third)]
        document.objects.append(obj)

        text = scene.dumps(document)
        assert text.index(OTHER) < text.index(third)


class TestStrictness:
    def test_an_unknown_document_key_is_refused(self):
        assert "unknown key 'extra'" in rejects("schema_version = 1\nextra = 1\n")

    def test_an_unknown_object_key_is_refused(self):
        text = f'schema_version = 1\n\n[[objects]]\nguid = "{GUID}"\nname = "x"\nnope = 1\n'
        assert "unknown key 'nope'" in rejects(text)

    def test_a_wrong_schema_version_names_the_number(self):
        assert "schema_version = 7" in rejects("schema_version = 7\n")

    def test_a_malformed_guid_is_refused(self):
        assert "must be a non-empty UUID" in rejects(
            'schema_version = 1\n\n[[objects]]\nguid = "nope"\nname = "x"\n'
        )

    def test_an_empty_name_is_refused(self):
        assert "non-empty 'name'" in rejects(
            f'schema_version = 1\n\n[[objects]]\nguid = "{GUID}"\nname = ""\n'
        )

    def test_a_duplicate_identity_is_refused(self):
        text = (
            "schema_version = 1\n"
            f'\n[[objects]]\nguid = "{GUID}"\nname = "a"\n'
            f'\n[[objects]]\nguid = "{GUID}"\nname = "b"\n'
        )
        assert "twice" in rejects(text)

    def test_a_dangling_parent_is_refused(self):
        # An edit that deleted an object without reparenting its children.
        text = f'schema_version = 1\n\n[[objects]]\nguid = "{GUID}"\nname = "x"\nparent = "{OTHER}"\n'
        assert "does not exist" in rejects(text)

    def test_a_parent_cycle_is_refused(self):
        # A cycle has no world transform at all; it must fail here, not as infinite recursion
        # while the loader walks the hierarchy.
        text = (
            "schema_version = 1\n"
            f'\n[[objects]]\nguid = "{GUID}"\nname = "a"\nparent = "{OTHER}"\n'
            f'\n[[objects]]\nguid = "{OTHER}"\nname = "b"\nparent = "{GUID}"\n'
        )
        assert "cycle" in rejects(text)

    def test_a_short_transform_array_is_refused(self):
        text = (
            f'schema_version = 1\n\n[[objects]]\nguid = "{GUID}"\nname = "x"\n'
            "\n[objects.transform]\nposition = [0.0, 1.0]\nrotation = [0.0, 0.0, 0.0, 1.0]\n"
            "scale = [1.0, 1.0, 1.0]\n"
        )
        assert "array of 3 numbers" in rejects(text)


class TestOpaquePayloads:
    def test_an_unrecognised_payload_survives_a_round_trip(self):
        # The property that makes it safe to open a scene full of components this addon has
        # never heard of.
        text = (
            "schema_version = 1\n"
            f'\n[[objects]]\nguid = "{GUID}"\nname = "x"\n'
            f'\n[[objects.components]]\nid = "{OTHER}"\ntype = "Nobody.Knows"\n'
            "\n[objects.components.data]\n"
            "Count = 3\n"
            "Ratio = 0.5\n"
            'Text = "hi"\n'
            "Flag = true\n"
            "List = [1, 2]\n"
        )
        assert scene.dumps(scene.loads(text, "x.scene")) == text
