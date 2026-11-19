"""Unit coverage for ``paradise_assets.document.mesh_slots`` (#67)."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest

from paradise_assets.document import canonical_toml, mesh_slots, prefab, project, schema, sidecar

MAT_0 = "41e306cc-9a00-4a55-8b76-8759d1d6ca4a"
MAT_1 = "b7fe765b-7873-4bfa-b6a0-add4b6d862d2"
MAT_2 = "691f026e-ca5c-4f87-9c01-71a0b4e5a72e"
MAT_3 = "0c118340-893b-45ce-b6d3-147f81d42532"
CUSTOM_MAT = "99999999-1111-4222-8333-444444444444"
ASSET_A = "1f249ff5-0206-4845-953b-d37629de70d5"
ASSET_B = "2973ffc8-6fa6-4266-ad50-7f34f21a96eb"
SOURCE_GUID = "fb564042-e90a-4666-84cb-5e0978cb281e"
MESH_GUID = "79ada386-950d-437b-8406-dfa391a63364"
STATIC_MESH_ID = "edee8bd8-9321-47db-819d-9bdadf010be4"


def _write_glb(path: Path, gltf_root: dict) -> None:
    raw = json.dumps(gltf_root).encode("utf-8")
    raw += b" " * (-len(raw) % 4)
    header = struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(raw))
    chunk = struct.pack("<I4s", len(raw), b"JSON") + raw
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + chunk)


def _slot(guid: str, name: str) -> canonical_toml.InlineTable:
    return canonical_toml.InlineTable({"guid": guid, "path": f"materials/{name}.material"})


def test_glb_draw_materials_walks_depth_first_in_scene_order() -> None:
    # Scene roots: [0, 3]. Node 0 has mesh 0 (materials 2, -1) and children [1, 2];
    # node 1 has no mesh and child []; node 2 has mesh 1 (material 0); node 3 has mesh 2 (material 1).
    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0, 3]}],
        "nodes": [
            {"name": "RootA", "mesh": 0, "children": [1, 2]},
            {"name": "EmptyChild"},
            {"name": "ChildMesh", "mesh": 1},
            {"name": "RootB", "mesh": 2},
        ],
        "meshes": [
            {"primitives": [{"material": 2}, {}]},
            {"primitives": [{"material": 0}]},
            {"primitives": [{"material": 1}]},
        ],
    }
    assert mesh_slots.glb_draw_materials(document) == (2, -1, 0, 1)


def test_glb_draw_materials_rejects_cycles_and_bad_indices() -> None:
    cyclic = {
        "asset": {"version": "2.0"},
        "scenes": [{"nodes": [0]}],
        "nodes": [{"children": [1]}, {"children": [0]}],
        "meshes": [],
    }
    assert mesh_slots.glb_draw_materials(cyclic) is None
    assert mesh_slots.glb_draw_materials({}) is None


def test_extracted_materials_scopes_by_asset_guid(tmp_path: Path) -> None:
    meta_path = tmp_path / "level.blend.meta"
    meta_path.write_text(
        'schema_version = 1\n'
        f'guid = "{SOURCE_GUID}"\n\n'
        '[extract]\n'
        'by = "glb"\n'
        f'parts = [{{ kind = "materials", index = 0, guid = "{MAT_0}", '
        f'path = "materials/body.material", asset = "{ASSET_A}" }}, '
        f'{{ kind = "materials", index = 0, guid = "{MAT_2}", '
        f'path = "materials/roof.material", asset = "{ASSET_B}" }}]\n',
        encoding="utf-8",
    )
    meta = sidecar.read(str(meta_path))
    assert mesh_slots.extracted_materials(meta, ASSET_A) == {0: _slot(MAT_0, "body")}
    assert mesh_slots.extracted_materials(meta, ASSET_B) == {0: _slot(MAT_2, "roof")}
    assert mesh_slots.extracted_materials(meta, None) == {}


def test_reconcile_slots_inserts_mid_list_draw_without_shifting_overrides() -> None:
    # Mirrors #67: M_Hidden (index 1) already exists at draw 0, a custom foreign override sits
    # on a later draw (draw 3), and M_Hidden is newly assigned to faces creating draw 1 ahead
    # of draw 2 (facade), shifting the override cleanly from index 3 to index 4.
    by_index = {
        0: _slot(MAT_0, "unassigned"),
        1: _slot(MAT_1, "hidden"),
        2: _slot(MAT_2, "facade"),
        3: _slot(MAT_3, "obstacle"),
    }
    ms = mesh_slots.ModelSlots(
        model=mesh_slots.model_source.Model("/tmp/level.blend", ASSET_A, "Level"),
        draw_materials=(1, 1, 2, 0, 0, 3),
        by_index=by_index,
    )
    override = _slot(CUSTOM_MAT, "custom_neon")
    # Previously 5 draws: (1, 2, 0, 0, 3) with draw 3 overridden to custom_neon
    stored = [
        _slot(MAT_1, "hidden"),
        _slot(MAT_2, "facade"),
        _slot(MAT_0, "unassigned"),
        override,
        _slot(MAT_3, "obstacle"),
    ]
    rebuilt, changed = mesh_slots.reconcile_slots(stored, ms)
    assert changed is True
    assert rebuilt == [
        _slot(MAT_1, "hidden"),
        _slot(MAT_1, "hidden"),
        _slot(MAT_2, "facade"),
        _slot(MAT_0, "unassigned"),
        override,
        _slot(MAT_3, "obstacle"),
    ]
    assert all(isinstance(s, canonical_toml.InlineTable) for s in rebuilt)

    # Fixed point on the second save, with the override intact:
    again, changed_again = mesh_slots.reconcile_slots(rebuilt, ms)
    assert changed_again is False
    assert again == rebuilt


def test_reconcile_slots_drops_removed_draw_and_handles_null_slot() -> None:
    by_index = {0: _slot(MAT_0, "wall"), 1: _slot(MAT_1, "trim")}
    ms = mesh_slots.ModelSlots(
        model=mesh_slots.model_source.Model("/tmp/prop.glb"),
        draw_materials=(0, -1),
        by_index=by_index,
    )
    stored = [_slot(MAT_0, "wall"), _slot(MAT_1, "trim"), canonical_toml.InlineTable()]
    rebuilt, changed = mesh_slots.reconcile_slots(stored, ms)
    assert changed is True
    assert rebuilt == [_slot(MAT_0, "wall"), canonical_toml.InlineTable()]


def test_reconcile_slots_refuses_unextracted_material_on_count_mismatch() -> None:
    ms = mesh_slots.ModelSlots(
        model=mesh_slots.model_source.Model("/tmp/assets/models/crate.glb"),
        draw_materials=(0, 1),
        by_index={0: _slot(MAT_0, "wood")},
    )
    with pytest.raises(ValueError, match="paradise assets extract"):
        mesh_slots.reconcile_slots([_slot(MAT_0, "wood")], ms, label="Crate")


def test_reconcile_entry_reads_multi_asset_converted_glb_and_round_trips_toml(tmp_path: Path) -> None:
    assets_dir = tmp_path / "assets"
    (assets_dir / "levels").mkdir(parents=True)
    (assets_dir / "meshes").mkdir(parents=True)
    (assets_dir / "project.toml").write_text('schema_version = 1\nname = "test"\n', encoding="utf-8")

    blend = assets_dir / "levels" / "scene.blend"
    blend_bytes = b"BLENDER-v502-stub"
    blend.write_bytes(blend_bytes)
    sha = hashlib.sha256(blend_bytes).hexdigest()

    (assets_dir / "levels" / "scene.blend.meta").write_text(
        f'schema_version = 1\nguid = "{SOURCE_GUID}"\n\n[extract]\nby = "glb"\n'
        f'parts = [{{ kind = "materials", index = 0, guid = "{MAT_0}", '
        f'path = "materials/base.material", asset = "{ASSET_A}" }}, '
        f'{{ kind = "materials", index = 1, guid = "{MAT_1}", '
        f'path = "materials/hidden.material", asset = "{ASSET_A}" }}]\n',
        encoding="utf-8",
    )
    (assets_dir / "meshes" / "scene.mesh").write_text(
        f'schema_version = 1\nsource = {{ guid = "{SOURCE_GUID}", path = "levels/scene.blend" }}\n'
        f'asset = {{ guid = "{ASSET_A}", name = "Scene" }}\nslot = "mesh"\n',
        encoding="utf-8",
    )

    layout = project.ProjectLayout(str(tmp_path))
    converted = Path(mesh_slots.model_source.converted_path(layout, str(blend), ASSET_A))
    _write_glb(
        converted,
        {
            "asset": {
                "version": "2.0",
                "extras": {
                    "paradiseSourceSha256": sha,
                    "paradiseDependencies": [],
                    "paradiseAsset": ASSET_A,
                },
            },
            "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}],
            "meshes": [{"primitives": [{"material": 0}, {"material": 1}, {}]}],
        },
    )

    doc_text = (
        "schema_version = 1\n\n[[objects]]\n\n"
        '[[objects.components]]\nid = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"\n'
        'type = "meta"\nGuid = "6ff014bc-1e30-4b06-8963-f76fff23e4e4"\nName = "Scene"\n\n'
        f'[[objects.components]]\nid = "{STATIC_MESH_ID}"\ntype = "Game.StaticMesh"\n'
        f'Mesh = {{ guid = "{MESH_GUID}", path = "meshes/scene.mesh" }}\n\n'
        f'[[objects.components]]\nid = "{mesh_slots.MATERIALS_ID}"\n'
        f'type = "{mesh_slots.MATERIALS_TYPE}"\n'
        f'Slots = [{{ guid = "{MAT_0}", path = "materials/base.material" }}, {{}}]\n'
    )
    document = prefab.loads(doc_text)
    fields = schema.MeshFields(None, None)

    warning = mesh_slots.stale_warning(document.objects[0], layout, fields)
    assert warning is not None and "2 slot(s)" in warning and "3 draw(s)" in warning

    changed = mesh_slots.reconcile_entry(document.objects[0], layout, fields)
    assert changed == 1
    dumped = prefab.dumps(document)
    reloaded = prefab.loads(dumped)
    slots = reloaded.objects[0].component(mesh_slots.MATERIALS_ID).data["Slots"]
    assert slots == [_slot(MAT_0, "base"), _slot(MAT_1, "hidden"), {}]
    assert mesh_slots.stale_warning(reloaded.objects[0], layout, fields) is None
