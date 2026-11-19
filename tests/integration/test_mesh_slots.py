"""Blender integration coverage for refreshing ``MaterialsComponentData.Slots`` on model draw changes (#67).

    blender --background --factory-startup --python tests/integration/test_mesh_slots.py
"""

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
from pathlib import Path

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import paradise_assets
from paradise_assets.document import mesh_slots, prefab, project, well_known
from paradise_assets.materialize import load, save, store

failures: list[str] = []

ROOT_GUID = "11111111-2222-4333-8444-555555555555"
SOURCE_GUID = "fb564042-e90a-4666-84cb-5e0978cb281e"
MESH_GUID = "79ada386-950d-437b-8406-dfa391a63364"
STATIC_MESH_ID = "edee8bd8-9321-47db-819d-9bdadf010be4"
MAT_WALL = "41e306cc-9a00-4a55-8b76-8759d1d6ca4a"
MAT_HIDDEN = "b7fe765b-7873-4bfa-b6a0-add4b6d862d2"
MAT_FLOOR = "59bf0103-f8b4-4c06-90f5-004b87b8bf2d"
MAT_TRIM = "691f026e-ca5c-4f87-9c01-71a0b4e5a72e"
MAT_CUSTOM = "99999999-1111-4222-8333-444444444444"


def check(condition: bool, label: str) -> bool:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)
    return condition


def write_glb(path: Path, material_indices: list[int]) -> None:
    """Write a minimal valid GLB with one triangle primitive per material index."""
    vertex_bytes = struct.pack("<9f", 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    primitives = [
        {"attributes": {"POSITION": 0}, **({"material": idx} if idx >= 0 else {})}
        for idx in material_indices
    ]
    gltf_doc = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"name": "Store", "mesh": 0}],
        "meshes": [{"name": "StoreMesh", "primitives": primitives}],
        "materials": [{"name": f"Mat_{i}"} for i in range(max([*material_indices, -1]) + 1)],
        "buffers": [{"byteLength": len(vertex_bytes)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(vertex_bytes)}],
        "accessors": [{
            "bufferView": 0,
            "componentType": 5126,
            "count": 3,
            "type": "VEC3",
            "min": [0.0, 0.0, 0.0],
            "max": [1.0, 1.0, 0.0],
        }],
    }
    raw_json = json.dumps(gltf_doc).encode("utf-8")
    raw_json += b" " * (-len(raw_json) % 4)
    total = 12 + 8 + len(raw_json) + 8 + len(vertex_bytes)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<I4s", len(raw_json), b"JSON") + raw_json
        + struct.pack("<I4s", len(vertex_bytes), b"BIN\x00") + vertex_bytes
    )


def make_project(root: Path) -> tuple[Path, Path]:
    assets = root / "assets"
    (assets / "levels").mkdir(parents=True)
    (assets / "models").mkdir(parents=True)
    (assets / "meshes").mkdir(parents=True)
    (assets / "materials").mkdir(parents=True)
    (root / ".editor").mkdir(parents=True)

    (assets / "project.toml").write_text('schema_version = 1\nname = "slotstest"\n', encoding="utf-8")
    (root / ".editor" / "authoring-schema.json").write_text(
        json.dumps({
            "components": [{
                "id": STATIC_MESH_ID,
                "type": "Game.StaticMesh",
                "fields": [{"name": "Mesh", "type": "asset", "authoredBy": "mesh"}],
            }]
        }),
        encoding="utf-8",
    )

    glb = assets / "models" / "store.glb"
    write_glb(glb, [0, 2, 3])
    (assets / "models" / "store.glb.meta").write_text(
        f'schema_version = 1\nguid = "{SOURCE_GUID}"\n\n[extract]\nby = "glb"\n'
        "parts = ["
        f'{{ kind = "materials", index = 0, guid = "{MAT_WALL}", path = "materials/wall.material" }}, '
        f'{{ kind = "materials", index = 1, guid = "{MAT_HIDDEN}", path = "materials/hidden.material" }}, '
        f'{{ kind = "materials", index = 2, guid = "{MAT_FLOOR}", path = "materials/floor.material" }}, '
        f'{{ kind = "materials", index = 3, guid = "{MAT_TRIM}", path = "materials/trim.material" }}]\n',
        encoding="utf-8",
    )
    (assets / "meshes" / "store.mesh").write_text(
        f'schema_version = 1\nsource = {{ guid = "{SOURCE_GUID}", path = "models/store.glb" }}\n'
        'slot = "mesh"\n',
        encoding="utf-8",
    )
    (assets / "meshes" / "store.mesh.meta").write_text(
        f'schema_version = 1\nguid = "{MESH_GUID}"\n', encoding="utf-8"
    )

    level = assets / "levels" / "entrance.prefab"
    level.write_text(
        "schema_version = 1\n\n[[objects]]\n\n"
        f'[[objects.components]]\nid = "{well_known.META_ID}"\ntype = "meta"\n'
        f'Guid = "{ROOT_GUID}"\nName = "Entrance"\n\n'
        f'[[objects.components]]\nid = "{well_known.TRANSFORM_ID}"\ntype = "transform"\n'
        "Position = [0, 0, 0]\nRotation = [0, 0, 0, 1]\nScale = [1, 1, 1]\n\n"
        f'[[objects.components]]\nid = "{STATIC_MESH_ID}"\ntype = "Game.StaticMesh"\n'
        f'Mesh = {{ guid = "{MESH_GUID}", path = "meshes/store.mesh" }}\n\n'
        f'[[objects.components]]\nid = "{mesh_slots.MATERIALS_ID}"\n'
        f'type = "{mesh_slots.MATERIALS_TYPE}"\n'
        f'Slots = [{{ guid = "{MAT_WALL}", path = "materials/wall.material" }}, '
        f'{{ guid = "{MAT_FLOOR}", path = "materials/floor.material" }}, '
        f'{{ guid = "{MAT_CUSTOM}", path = "materials/custom_trim.material" }}]\n',
        encoding="utf-8",
    )
    return level, glb


def main() -> int:
    paradise_assets.register()
    try:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            level, glb = make_project(root)
            layout = project.ProjectLayout(str(root))

            print("\n== an in-sync model with an authored slot override saves byte-identically ==")
            bpy.ops.wm.read_factory_settings(use_empty=True)
            before = level.read_text(encoding="utf-8")
            loaded = load.load_document(
                bpy.context.scene, prefab.loads(before, str(level)), str(level), layout
            )
            check(not loaded.warnings, f"no warnings when Slots matches draw count ({loaded.warnings})")
            save.save_prefab(bpy.context.scene)
            check(level.read_text(encoding="utf-8") == before,
                  "unchanged draw count preserves authored override and rewrites zero bytes")

            print("\n== inserting a primitive mid-list warns on load and updates Slots on save ==")
            # Repro of #67: an existing material (index 1, MAT_HIDDEN) is assigned to faces,
            # inserting a draw at index 1 ahead of MAT_FLOOR and the overridden trim draw.
            write_glb(glb, [0, 1, 2, 3])
            bpy.ops.wm.read_factory_settings(use_empty=True)
            loaded = load.load_document(
                bpy.context.scene, prefab.loads(level.read_text(encoding="utf-8"), str(level)),
                str(level), layout,
            )
            check(any("3 slot(s)" in w and "4 draw(s)" in w for w in loaded.warnings),
                  f"load warns about stale Slots count ({loaded.warnings})")

            result = save.save_prefab(bpy.context.scene)
            check(result.edited >= 1, f"save records the reconciled Slots edit ({result.edited})")
            written_text = level.read_text(encoding="utf-8")
            written = prefab.loads(written_text, str(level))
            slots = written.objects[0].component(mesh_slots.MATERIALS_ID).data["Slots"]
            check(
                [s.get("guid") for s in slots] == [MAT_WALL, MAT_HIDDEN, MAT_FLOOR, MAT_CUSTOM],
                f"inserted draw gets MAT_HIDDEN and authored override shifts to draw 3 ({slots})",
            )
            obj = store.object_with_guid(bpy.context.scene, ROOT_GUID)
            shown_slots = next(
                c["data"]["Slots"] for c in store.component_json(obj)
                if c["id"].lower() == mesh_slots.MATERIALS_ID
            )
            check(len(shown_slots) == 4, "post-save snapshot refreshes to 4 slots")

            save.save_prefab(bpy.context.scene)
            check(level.read_text(encoding="utf-8") == written_text,
                  "second save of the reconciled document is byte-identical")

            print("\n== an unextracted material on a new draw refuses the save with instructions ==")
            write_glb(glb, [0, 1, 2, 3, 4])   # material index 4 is not in store.glb.meta [extract]
            refused = None
            try:
                save.save_prefab(bpy.context.scene)
            except save.SaveError as error:
                refused = str(error)
            check(
                refused is not None and "paradise assets extract" in refused,
                f"save refuses unextracted draw material with CLI hint ({refused})",
            )
    finally:
        paradise_assets.unregister()

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
