"""Tests for listing pickable project assets from sidecars."""

from __future__ import annotations

import os

from paradise_assets.document import assets as asset_index
from paradise_assets.document.project import ProjectLayout


def _project(tmp_path, files: dict[str, str]) -> ProjectLayout:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "project.toml").write_text("name = \"test\"\n", encoding="utf-8")
    for relative, guid in files.items():
        path = assets / relative.replace("/", os.sep)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("name = \"x\"\n", encoding="utf-8")
        path.with_name(path.name + ".meta").write_text(
            f"schema_version = 1\nguid = \"{guid}\"\n", encoding="utf-8")
    return ProjectLayout(str(tmp_path))


def test_list_assets_reads_sidecar_guids_and_authoring_paths(tmp_path):
    layout = _project(tmp_path, {
        "materials/car.toml": "11111111-1111-4111-8111-111111111111",
        "materials/wood.toml": "22222222-2222-4222-8222-222222222222",
        "models/box.glb": "33333333-3333-4333-8333-333333333333",
    })

    tomls = asset_index.list_assets(layout, [".toml"])

    assert [item.path for item in tomls] == ["materials/car.toml", "materials/wood.toml"]
    assert tomls[0].guid == "11111111-1111-4111-8111-111111111111"


def test_list_assets_skips_the_manifest_and_files_without_a_sidecar(tmp_path):
    layout = _project(tmp_path, {"materials/car.toml": "11111111-1111-4111-8111-111111111111"})
    (tmp_path / "assets" / "orphan.toml").write_text("name = \"no\"\n", encoding="utf-8")

    found = asset_index.list_assets(layout, [".toml"])

    assert [item.path for item in found] == ["materials/car.toml"]
