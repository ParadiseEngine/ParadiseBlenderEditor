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


def test_nested_and_list_fields_are_not_editable():
    # Not a refusal on principle -- they are simply shapes the panel cannot address yet. A nested
    # payload needs a value addressed by PATH, and a list needs add/remove/reorder before an edit
    # means anything. Both stay read-only, which is what every field was before.
    root = _project([{
        "id": "33333333-3333-4333-8333-333333333333",
        "type": "Game.Nested",
        "fields": [
            {"name": "Group", "type": "object", "fields": []},
            {"name": "Slots", "type": "array", "items": {"type": "string"}},
        ],
    }])

    schema = component_schema.load(root).get("33333333-3333-4333-8333-333333333333")

    assert not schema.field("Group").editable
    assert not schema.field("Slots").editable


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
