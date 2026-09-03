"""The mesh-field schema reader shares the schema dump's location list with the component
vocabulary: ``.editor/`` first, since that is where a launcher build writes it now."""

from __future__ import annotations

import json
import os

from paradise_assets.document import project, schema


def _dump(root, directory, pairs):
    os.makedirs(os.path.join(root, directory), exist_ok=True)
    with open(os.path.join(root, directory, "authoring-schema.json"), "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "components": [
            {"id": "b7ab4dd8-c8da-4dc2-9e5e-192fd74deb11", "type": component,
             "fields": [{"name": field, "type": "asset", "authoredBy": "mesh"}]}
            for component, field in pairs
        ]}, handle)


def test_the_editor_cache_dump_wins_over_the_older_layouts(tmp_path):
    root = str(tmp_path)
    _dump(root, "build", [("Game.Old", "Mesh")])
    _dump(root, ".editor", [("Game.Fresh", "Mesh")])

    fields = schema.load(root)

    assert fields.source == os.path.join(root, ".editor", "authoring-schema.json")
    assert fields.is_mesh_field("Game.Fresh", "Mesh", "x.glb")
    assert not fields.is_mesh_field("Game.Old", "Mesh", "x.glb")


def test_without_a_dump_the_fallback_applies(tmp_path):
    fields = schema.load(str(tmp_path))

    assert fields.source is None


def test_both_readers_share_one_candidate_list():
    assert project.SCHEMA_CANDIDATES[0] == ".editor/authoring-schema.json"
    assert schema.SCHEMA_CANDIDATES is project.SCHEMA_CANDIDATES
