"""Unit coverage for ``paradise_assets.document.detach_model`` (#66)."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest

from paradise_assets.document import (
    canonical_toml,
    detach_model,
    mesh_slots,
    model_source,
    prefab,
    project,
    sidecar,
    well_known,
)

SOURCE_GUID = "fb564042-e90a-4666-84cb-5e0978cb281e"
BODY_ASSET = "1f249ff5-0206-4845-953b-d37629de70d5"
ROOF_A_ASSET = "2973ffc8-6fa6-4266-ad50-7f34f21a96eb"
ROOF_B_ASSET = "4c611368-9ecb-4177-8136-b8f4646b42c3"
BODY_MESH_GUID = "79ada386-950d-437b-8406-dfa391a63364"
STATIC_MESH_ID = "edee8bd8-9321-47db-819d-9bdadf010be4"
MAT_WALL = "41e306cc-9a00-4a55-8b76-8759d1d6ca4a"
MAT_CONCRETE = "f641e20d-a37c-474b-b0a5-bb272f34baf7"
MAT_FACADE = "691f026e-ca5c-4f87-9c01-71a0b4e5a72e"
MAT_ROOF_A = "668372de-78c5-4526-892d-137435e77e77"
MAT_ROOF_B = "f889a8ee-697d-4945-acae-58ac6dc10f7f"
MAT_CUSTOM = "99999999-1111-4222-8333-444444444444"
LEVEL_A_ROOT = "6ff014bc-1e30-4b06-8963-f76fff23e4e4"
LEVEL_B_ROOT = "aaaaaa00-1e30-4b06-8963-f76fff23e4e4"


def _write_stamped_glb(path: Path, source_sha: str, asset_guid: str, mat_indices: list[int]) -> None:
    doc = {
        "asset": {
            "version": "2.0",
            "extras": {
                "paradiseSourceSha256": source_sha,
                "paradiseDependencies": [],
                "paradiseAsset": asset_guid,
            },
        },
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [
            {
                "primitives": [
                    {"attributes": {"POSITION": 0}, **({"material": m} if m >= 0 else {})}
                    for m in mat_indices
                ]
            }
        ],
    }
    raw = json.dumps(doc).encode("utf-8")
    raw += b" " * (-len(raw) % 4)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, 20 + len(raw))
        + struct.pack("<I4s", len(raw), b"JSON")
        + raw
    )


def _setup_project(tmp_path: Path) -> tuple[project.ProjectLayout, Path, Path, Path]:
    assets = tmp_path / "assets"
    for sub in ("levels", "meshes", "materials", "prefabs"):
        (assets / sub).mkdir(parents=True)
    (tmp_path / ".editor").mkdir()

    (assets / "project.toml").write_text(
        'schema_version = 1\nname = "detachtest"\n\n'
        '[extract]\nmeshes = "meshes"\nmaterials = "materials"\nprefabs = "prefabs"\n',
        encoding="utf-8",
    )
    (tmp_path / ".editor" / "authoring-schema.json").write_text(
        json.dumps(
            {
                "components": [
                    {
                        "id": STATIC_MESH_ID,
                        "type": "ShiningPie.Authoring.StaticMesh",
                        "fields": [{"name": "Mesh", "type": "asset", "authoredBy": "mesh"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    blend = assets / "levels" / "L_TWLT.level.blend"
    blend.write_bytes(b"BLENDER-v502-whole")
    (assets / "levels" / "L_TWLT.level.blend.meta").write_text(
        f'schema_version = 1\nguid = "{SOURCE_GUID}"\nimporter = "glb"\n\n'
        "[extract]\nby = \"glb\"\nparts = ["
        f'{{ kind = "materials", ownership = "two-sided", index = 0, name = "Wall", '
        f'guid = "{MAT_WALL}", path = "materials/L_TWLT.level.Wall.material" }}, '
        f'{{ kind = "materials", ownership = "two-sided", index = 1, name = "Concrete", '
        f'guid = "{MAT_CONCRETE}", path = "materials/L_TWLT.level.Concrete.material" }}, '
        f'{{ kind = "materials", ownership = "two-sided", index = 2, name = "Facade", '
        f'guid = "{MAT_FACADE}", path = "materials/L_TWLT.level.Facade.material" }}, '
        f'{{ kind = "meshes", ownership = "tool", index = 0, name = "L_TWLT.level", '
        f'guid = "{BODY_MESH_GUID}", path = "meshes/L_TWLT.level.mesh" }}]\n',
        encoding="utf-8",
    )
    (assets / "meshes" / "L_TWLT.level.mesh").write_text(
        f'schema_version = 1\nsource = {{ guid = "{SOURCE_GUID}", path = "levels/L_TWLT.level.blend" }}\n'
        'slot = "mesh"\n',
        encoding="utf-8",
    )
    (assets / "meshes" / "L_TWLT.level.mesh.meta").write_text(
        f'schema_version = 1\nguid = "{BODY_MESH_GUID}"\nimporter = "mesh"\n',
        encoding="utf-8",
    )

    level_a = assets / "levels" / "L_TWLT.level.prefab"
    level_a.write_text(
        "schema_version = 1\n\n[[objects]]\n\n"
        f'[[objects.components]]\nid = "{well_known.META_ID}"\ntype = "meta"\n'
        f'Guid = "{LEVEL_A_ROOT}"\nName = "L_TWLT.level"\n\n'
        f'[[objects.components]]\nid = "{STATIC_MESH_ID}"\ntype = "ShiningPie.Authoring.StaticMesh"\n'
        f'Mesh = {{ guid = "{BODY_MESH_GUID}", path = "meshes/L_TWLT.level.mesh" }}\n\n'
        f'[[objects.components]]\nid = "{mesh_slots.MATERIALS_ID}"\n'
        f'type = "{mesh_slots.MATERIALS_TYPE}"\n'
        "Slots = ["
        f'{{ guid = "{MAT_WALL}", path = "materials/L_TWLT.level.Wall.material" }}, '
        f'{{ guid = "{MAT_CUSTOM}", path = "materials/custom_concrete.material" }}, '
        f'{{ guid = "{MAT_CONCRETE}", path = "materials/L_TWLT.level.Concrete.material" }}, '
        f'{{ guid = "{MAT_FACADE}", path = "materials/L_TWLT.level.Facade.material" }}]\n\n'
        f'[[objects.components]]\nid = "{well_known.TRANSFORM_ID}"\ntype = "transform"\n'
        "Position = [6.0, 0.0, 3.0]\nRotation = [0.0, 0.0, 0.0, 1.0]\nScale = [1.0, 1.0, 1.0]\n",
        encoding="utf-8",
    )

    level_b = assets / "levels" / "L_TWLT_Mirror.prefab"
    level_b.write_text(
        "schema_version = 1\n\n[[objects]]\n\n"
        f'[[objects.components]]\nid = "{well_known.META_ID}"\ntype = "meta"\n'
        f'Guid = "{LEVEL_B_ROOT}"\nName = "MirrorBody"\n\n'
        f'[[objects.components]]\nid = "{STATIC_MESH_ID}"\ntype = "ShiningPie.Authoring.StaticMesh"\n'
        f'Mesh = {{ guid = "{BODY_MESH_GUID}", path = "meshes/L_TWLT.level.mesh" }}\n\n'
        f'[[objects.components]]\nid = "{mesh_slots.MATERIALS_ID}"\n'
        f'type = "{mesh_slots.MATERIALS_TYPE}"\n'
        "Slots = ["
        f'{{ guid = "{MAT_WALL}", path = "materials/L_TWLT.level.Wall.material" }}, '
        f'{{ guid = "{MAT_CONCRETE}", path = "materials/L_TWLT.level.Concrete.material" }}, '
        f'{{ guid = "{MAT_CONCRETE}", path = "materials/L_TWLT.level.Concrete.material" }}, '
        f'{{ guid = "{MAT_FACADE}", path = "materials/L_TWLT.level.Facade.material" }}]\n',
        encoding="utf-8",
    )

    return project.ProjectLayout(str(tmp_path)), blend, level_a, level_b


def test_clean_and_document_names() -> None:
    assert detach_model.is_document_name("Arcade_Roof")
    assert detach_model.is_document_name("L_TWLT.level")
    assert not detach_model.is_document_name("")
    assert not detach_model.is_document_name("bad.")
    assert not detach_model.is_document_name("a/b")
    assert detach_model.clean_model_name("  Roof:North/01. ") == "Roof_North_01"


def test_preflight_refuses_collisions_up_front(tmp_path: Path) -> None:
    layout, blend, _level_a, _level_b = _setup_project(tmp_path)

    # Duplicate target names (case-insensitive)
    with pytest.raises(detach_model.DetachError, match="colliding model names"):
        detach_model.preflight_targets(
            layout, str(blend), None, [], "L_TWLT.level", ["Arcade_Roof", "arcade_roof"]
        )

    # Collision with remainder stem
    with pytest.raises(detach_model.DetachError, match="collides with the remainder"):
        detach_model.preflight_targets(
            layout, str(blend), None, [], "L_TWLT.level", ["L_TWLT.level"]
        )

    # Collision with existing collection
    with pytest.raises(detach_model.DetachError, match=r"already exists in this \.blend"):
        detach_model.preflight_targets(
            layout, str(blend), None, ["Props"], "L_TWLT.level", ["props"]
        )

    # Collision in extraction namespace (materials/Arcade_Roof.wall.material already exists)
    existing_mat = tmp_path / "assets" / "materials" / "Arcade_Roof.wall.material"
    existing_mat.write_text("Name = 'wall'\n", encoding="utf-8")
    with pytest.raises(detach_model.DetachError, match="extraction namespace"):
        detach_model.preflight_targets(
            layout, str(blend), None, [], "L_TWLT.level", ["Arcade_Roof"]
        )


def test_full_detach_document_flow_preserves_identities_and_fans_out(tmp_path: Path) -> None:
    layout, blend, level_a, level_b = _setup_project(tmp_path)
    plan = detach_model.preflight_targets(
        layout, str(blend), None, [], "L_TWLT.level", ["Arcade_Roof", "Store_Roof"]
    )
    assert set(plan.placing_prefabs) == {str(level_a), str(level_b)}

    # Simulate saving restructured .blend + converting 3 asset collections
    # Body kept draws [0, 0] (the two roofs with MAT_CONCRETE and MAT_FACADE were detached)
    blend_bytes = b"BLENDER-v502-split"
    blend.write_bytes(blend_bytes)
    sha = hashlib.sha256(blend_bytes).hexdigest()
    _write_stamped_glb(
        Path(model_source.converted_path(layout, str(blend), BODY_ASSET)), sha, BODY_ASSET, [0, 0]
    )
    _write_stamped_glb(
        Path(model_source.converted_path(layout, str(blend), ROOF_A_ASSET)), sha, ROOF_A_ASSET, [0]
    )
    _write_stamped_glb(
        Path(model_source.converted_path(layout, str(blend), ROOF_B_ASSET)), sha, ROOF_B_ASSET, [0, -1]
    )

    # Step 1: Migrate remainder attribution
    detach_model.migrate_remainder_attribution(plan, BODY_ASSET, "L_TWLT.level")
    body_mesh_parsed = canonical_toml.loads(
        (tmp_path / "assets" / "meshes" / "L_TWLT.level.mesh").read_text(encoding="utf-8")
    )
    assert body_mesh_parsed["asset"] == {"guid": BODY_ASSET, "name": "L_TWLT.level"}
    assert list(body_mesh_parsed.keys()) == ["schema_version", "source", "asset", "slot"]

    meta_before = sidecar.read(sidecar.path_for(str(blend)))
    assert all(p.get("asset") == BODY_ASSET for p in meta_before.setting("extract")["parts"])

    # Step 2: Stage detached .mesh + .mesh.meta + direct placements before extract
    detached_meshes, staged = detach_model.stage_detached_meshes_and_placements(
        plan, [("Arcade_Roof", ROOF_A_ASSET), ("Store_Roof", ROOF_B_ASSET)]
    )
    assert [dm.mesh_rel for dm in detached_meshes] == [
        "meshes/Arcade_Roof.mesh",
        "meshes/Store_Roof.mesh",
    ]
    for dm in detached_meshes:
        assert sidecar.read(dm.mesh_abs + ".meta").guid == dm.mesh_guid

    # Both placing prefabs already reference the new mesh GUIDs BEFORE extract runs!
    for level_path in (level_a, level_b):
        pre_doc = prefab.loads(level_path.read_text(encoding="utf-8"))
        referenced_guids = {
            obj.component(STATIC_MESH_ID).data["Mesh"]["guid"]
            for obj in pre_doc.objects
            if obj.component(STATIC_MESH_ID) is not None
        }
        assert referenced_guids == {
            BODY_MESH_GUID,
            detached_meshes[0].mesh_guid,
            detached_meshes[1].mesh_guid,
        }

    # Simulate `paradise assets extract`: appends the new assets' material parts to <blend>.meta
    meta_path = Path(sidecar.path_for(str(blend)))
    meta_table = canonical_toml.loads(meta_path.read_text(encoding="utf-8"))
    parts = [canonical_toml.InlineTable(p) for p in meta_table["extract"]["parts"]]
    parts.extend(
        [
            canonical_toml.InlineTable(
                {
                    "kind": "materials",
                    "index": 0,
                    "name": "Concrete",
                    "guid": MAT_ROOF_A,
                    "path": "materials/Arcade_Roof.Concrete.material",
                    "asset": ROOF_A_ASSET,
                }
            ),
            canonical_toml.InlineTable(
                {
                    "kind": "materials",
                    "index": 0,
                    "name": "Facade",
                    "guid": MAT_ROOF_B,
                    "path": "materials/Store_Roof.Facade.material",
                    "asset": ROOF_B_ASSET,
                }
            ),
        ]
    )
    meta_table["extract"]["parts"] = parts
    meta_path.write_text(canonical_toml.dumps(meta_table), encoding="utf-8")

    # Step 3: Finalize placements (populate detached Slots and reconcile body Slots)
    detach_model.finalize_placements(plan, staged)

    doc_a = prefab.loads(level_a.read_text(encoding="utf-8"))
    assert len(doc_a.objects) == 3
    body_a, roof_a, store_a = doc_a.objects
    # Remainder's custom concrete override at draw 1 survived shrinking from 4 draws to 2!
    assert [s.get("guid") for s in body_a.component(mesh_slots.MATERIALS_ID).data["Slots"]] == [
        MAT_WALL,
        MAT_CUSTOM,
    ]
    assert roof_a.parent == LEVEL_A_ROOT
    assert roof_a.name == "Arcade_Roof"
    assert roof_a.component(well_known.TRANSFORM_ID).data["Position"] == [0.0, 0.0, 0.0]
    assert roof_a.component( mesh_slots.MATERIALS_ID).data["Slots"] == [
        {"guid": MAT_ROOF_A, "path": "materials/Arcade_Roof.Concrete.material"}
    ]
    assert store_a.parent == LEVEL_A_ROOT
    assert store_a.component(mesh_slots.MATERIALS_ID).data["Slots"] == [
        {"guid": MAT_ROOF_B, "path": "materials/Store_Roof.Facade.material"},
        {},
    ]

    doc_b = prefab.loads(level_b.read_text(encoding="utf-8"))
    assert len(doc_b.objects) == 3
    assert [o.parent for o in doc_b.objects[1:]] == [LEVEL_B_ROOT, LEVEL_B_ROOT]
