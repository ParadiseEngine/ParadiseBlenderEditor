from __future__ import annotations

import json

import pytest

from paradise_assets.document.geometry_prefab import prepare
from paradise_assets.document.new_prefab import CreateError
from paradise_assets.document.project import ProjectLayout


def project(tmp_path, extraction=""):
    layout = ProjectLayout(str(tmp_path))
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/project.toml").write_text('schema_version = 1\n' + extraction)
    (tmp_path / ".editor").mkdir()
    (tmp_path / ".editor/authoring-schema.json").write_text(json.dumps({"components": [{
        "id": "22222222-2222-4222-8222-222222222222", "type": "Game.StaticMesh",
        "fields": [{"name": "Mesh", "authoredBy": "mesh"}],
    }]}))
    return layout


def test_preflight_respects_per_kind_and_fallback_directories(tmp_path):
    layout = project(tmp_path, '[extract]\ndirectory = "parts"\nprefabs = "seeds"\n')
    target = prepare(layout.resolve("prefabs/Crate.prefab"), layout)
    assert target.model == layout.resolve("prefabs/Crate.glb")
    assert target.seed == layout.resolve("seeds/Crate.prefab")
    assert not (tmp_path / "assets/prefabs").exists()


@pytest.mark.parametrize("name", ["Crate.mesh", "Crate.Red.material", "Crate_0.png", "Crate.mesh.meta"])
def test_preflight_refuses_existing_extracted_assets_before_writing(tmp_path, name):
    layout = project(tmp_path, '[extract]\ndirectory = "parts"\n')
    (tmp_path / "assets/parts").mkdir()
    (tmp_path / "assets/parts" / name).write_text("author's file")
    with pytest.raises(CreateError, match="already exists"):
        prepare(layout.resolve("prefabs/Crate.prefab"), layout)
    assert not (tmp_path / "assets/prefabs").exists()


def test_preflight_refuses_missing_game_schema(tmp_path):
    layout = project(tmp_path)
    (tmp_path / ".editor/authoring-schema.json").unlink()
    with pytest.raises(CreateError, match="launcher"):
        prepare(layout.resolve("Crate.prefab"), layout)


def test_preflight_refuses_extraction_outside_assets(tmp_path):
    layout = project(tmp_path, '[extract]\nmaterials = "../outside"\n')
    with pytest.raises(CreateError, match="outside"):
        prepare(layout.resolve("Crate.prefab"), layout)


def test_prefab_cannot_escape_through_an_assets_symlink(tmp_path):
    layout = project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "assets/linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CreateError, match="outside"):
        prepare(layout.resolve("linked/Crate.prefab"), layout)
