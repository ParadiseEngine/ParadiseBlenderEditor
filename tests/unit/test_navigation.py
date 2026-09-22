"""Generated navigation paths belong to their level and never replace unrelated payloads."""

from __future__ import annotations

import copy
import json

import pytest

from paradise_assets.document import component_schema, navigation, prefab

NAVIGATION = "11111111-1111-4111-8111-111111111111"
UNKNOWN = "22222222-2222-4222-8222-222222222222"


@pytest.mark.parametrize("level, expected", [
    ("levels/battlefield.prefab", "levels/battlefield.navmesh"),
    ("levels/city/night.market.prefab", "levels/city/night.market.navmesh"),
    ("levels/test.toml", "levels/test.navmesh"),
])
def test_generated_path_is_beside_level(tmp_path, level, expected):
    assets = tmp_path / "assets"
    assert navigation.asset_path(str(assets / level), str(assets)) == expected


def test_level_outside_assets_is_refused_including_symlinks(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (assets / "link").symlink_to(outside, target_is_directory=True)
    for path in (outside / "scene.prefab", assets / "../scene.prefab", assets / "link/scene.prefab"):
        with pytest.raises(ValueError, match="inside.*assets"):
            navigation.asset_path(str(path), str(assets))


def vocabulary(tmp_path):
    cache = tmp_path / ".editor"
    cache.mkdir()
    (cache / "authoring-schema.json").write_text(json.dumps({"components": [{
        "id": NAVIGATION, "type": "Test.Navigation", "fields": [
            {"name": "NavMeshFile", "type": "string", "authoredBy": "navmesh",
             "assetKinds": [".navmesh"]},
            {"name": "Custom", "type": "string"},
        ],
    }]}))
    return component_schema.load(str(tmp_path))


def test_navmesh_field_cannot_offer_text_or_asset_editors(tmp_path):
    schema = vocabulary(tmp_path).get(NAVIGATION)
    field = schema.field("NavMeshFile")
    assert not field.editable
    assert not component_schema.is_asset_field(field)
    assert schema.plan({"NavMeshFile": "obsolete.bin"})[0].role == component_schema.ROLE_LOCKED


def test_normalization_preserves_unknown_and_removed_payloads(tmp_path):
    known = prefab.PrefabComponent(NAVIGATION, data={
        "NavMeshFile": "shared/obsolete.bin", "Custom": "unchanged",
        "Unknown": {"Precision": 0.12345678901234567, "Flags": [1, 2, 3]},
    })
    unknown = prefab.PrefabComponent(UNKNOWN, data={"NavMeshFile": "leave-me.bin"})
    removed = prefab.PrefabComponent(NAVIGATION, data={"NavMeshFile": "removed.bin"}, removed=True)
    document = prefab.PrefabDocument([prefab.PrefabObject(components=[known, unknown, removed])])
    before = copy.deepcopy(document)
    schemas = vocabulary(tmp_path)
    assets = tmp_path / "assets"
    level = assets / "levels/nested/arena.prefab"

    assert navigation.normalize(document, schemas, str(level), str(assets)) == 1
    assert known.data["NavMeshFile"] == "levels/nested/arena.navmesh"
    before.objects[0].components[0].data["NavMeshFile"] = "levels/nested/arena.navmesh"
    assert document == before
    assert navigation.normalize(document, schemas, str(level), str(assets)) == 0
    assert navigation.normalize(document, schemas, str(level.with_name("renamed.prefab")), str(assets)) == 1
    assert known.data["NavMeshFile"] == "levels/nested/renamed.navmesh"
