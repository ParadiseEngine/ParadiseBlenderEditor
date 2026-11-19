"""Blender integration test for detaching selected objects of a model source into separate models (#66).

    blender --background --factory-startup --python tests/integration/test_detach_model.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import paradise_assets
from paradise_assets.document import (
    detach_model,
    mesh_slots,
    prefab,
    project,
    sidecar,
    well_known,
)
from paradise_assets.materialize import load, save
from paradise_assets.play import host

failures: list[str] = []

SOURCE_GUID = "fb564042-e90a-4666-84cb-5e0978cb281e"
BODY_MESH_GUID = "79ada386-950d-437b-8406-dfa391a63364"
STATIC_MESH_ID = "edee8bd8-9321-47db-819d-9bdadf010be4"
MAT_WALL = "41e306cc-9a00-4a55-8b76-8759d1d6ca4a"
MAT_ROOF_1 = "f641e20d-a37c-474b-b0a5-bb272f34baf7"
MAT_ROOF_2 = "691f026e-ca5c-4f87-9c01-71a0b4e5a72e"
LEVEL_A_GUID = "6ff014bc-1e30-4b06-8963-f76fff23e4e4"
LEVEL_B_GUID = "7ff014bc-1e30-4b06-8963-f76fff23e4e4"

#: Faithful fake CLI enforcing BlenderModelConverter + AssetExtractor.RemoveMissingModels + Seed + Verify.
FAKE_CLI = '''#!/usr/bin/env python3
from __future__ import annotations
import hashlib, json, os, struct, subprocess, sys, tomllib, uuid
from pathlib import Path

argv = sys.argv[1:]
proj_idx = argv.index("--project")
root = Path(argv[proj_idx + 1])
assets = root / "assets"
editor = root / ".editor"
blender_bin = os.environ["TEST_BLENDER_BIN"]
repo_root = os.environ["TEST_REPO_ROOT"]
sys.path.insert(0, repo_root)
from paradise_assets.document import canonical_toml, prefab, sidecar

verb = argv[:2]

INSPECT_BLEND = """
import bpy, json, os, sys
out_dir, list_out = sys.argv[sys.argv.index("--") + 1:][:2]
cols = sorted(
    [c for c in bpy.data.collections if c.library is None and c.asset_data is not None],
    key=lambda c: c.name,
)
records = []
for col in cols:
    guid = col.get("paradise_guid")
    if not isinstance(guid, str) or len(guid) != 36:
        sys.exit(f"missing paradise_guid on {col.name}")
    for child in col.children_recursive:
        if child.asset_data is not None:
            sys.exit(f"nested asset collection {child.name} in {col.name}")
    glb_path = os.path.join(out_dir, f"{guid}.glb")
    scene = bpy.context.scene
    vl = bpy.context.view_layer
    linked = col.name not in [c.name for c in scene.collection.children_recursive]
    if linked:
        scene.collection.children.link(col)
    def find_lc(lc):
        if lc.collection == col:
            return lc
        for ch in lc.children:
            r = find_lc(ch)
            if r is not None:
                return r
        return None
    vl.active_layer_collection = find_lc(vl.layer_collection)
    bpy.ops.export_scene.gltf(
        filepath=glb_path,
        export_format="GLB",
        export_yup=True,
        export_apply=True,
        use_active_scene=True,
        use_active_collection=True,
        use_active_collection_with_nested=True,
    )
    if linked:
        scene.collection.children.unlink(col)
    records.append({"guid": guid, "name": col.name, "glb": glb_path})
with open(list_out, "w", encoding="utf-8") as f:
    json.dump(records, f)
"""

def stamp_glb(glb_path: Path, sha: str, asset_guid: str, asset_name: str, all_assets: list[dict]) -> None:
    raw = glb_path.read_bytes()
    magic, ver, _total = struct.unpack_from("<4sII", raw, 0)
    assert magic == b"glTF"
    j_len, j_kind = struct.unpack_from("<I4s", raw, 12)
    doc = json.loads(raw[20:20 + j_len].decode("utf-8"))
    rest = raw[20 + j_len:]
    doc.setdefault("asset", {})["extras"] = {
        "paradiseSourceSha256": sha,
        "paradiseDependencies": [],
        "paradiseAsset": asset_guid,
        "paradiseAssetName": asset_name,
        "paradiseAssets": all_assets,
    }
    j_bytes = json.dumps(doc).encode("utf-8")
    j_bytes += b" " * (-len(j_bytes) % 4)
    out = (
        struct.pack("<4sII", b"glTF", ver, 12 + 8 + len(j_bytes) + len(rest))
        + struct.pack("<I4s", len(j_bytes), b"JSON") + j_bytes + rest
    )
    glb_path.write_bytes(out)

if verb == ["assets", "convert"]:
    source = Path(argv[2]).resolve()
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    rel = source.relative_to(assets)
    conv_dir = editor / "converted" / rel
    conv_dir.mkdir(parents=True, exist_ok=True)
    list_file = conv_dir / "assets.json"
    run = subprocess.run(
        [blender_bin, "--background", "--factory-startup", "--disable-autoexec",
         "--python-exit-code", "1", str(source),
         "--python-expr", INSPECT_BLEND, "--", str(conv_dir), str(list_file)],
        capture_output=True, text=True,
    )
    if run.returncode != 0:
        sys.stderr.write(run.stdout + run.stderr)
        sys.exit(1)
    records = json.loads(list_file.read_text(encoding="utf-8"))
    all_assets = [{"guid": r["guid"], "name": r["name"]} for r in records]
    for r in records:
        stamp_glb(Path(r["glb"]), sha, r["guid"], r["name"], all_assets)
        print(r["glb"])
    sys.exit(0)

if verb == ["assets", "extract"]:
    source = Path(argv[2]).resolve()
    rel = source.relative_to(assets)
    conv_dir = editor / "converted" / rel
    records = json.loads((conv_dir / "assets.json").read_text(encoding="utf-8"))
    valid_assets = {r["guid"] for r in records}
    meta_path = Path(str(source) + ".meta")
    meta_table = canonical_toml.loads(meta_path.read_text(encoding="utf-8"))
    source_guid = meta_table["guid"]

    # Enforce AssetExtractor.RemoveMissingModels: any .mesh without matching asset GUID is deleted!
    for mfile in list((assets / "meshes").glob("*.mesh")):
        mdata = canonical_toml.loads(mfile.read_text(encoding="utf-8"))
        if mdata.get("source", {}).get("guid") == source_guid:
            ag = mdata.get("asset", {}).get("guid") if isinstance(mdata.get("asset"), dict) else None
            if ag not in valid_assets:
                mfile.unlink()
                meta_m = Path(str(mfile) + ".meta")
                if meta_m.exists():
                    meta_m.unlink()

    existing_parts = [
        p for p in meta_table.get("extract", {}).get("parts", [])
        if p.get("asset") in valid_assets
    ]
    by_asset_name = {
        (p.get("asset"), p.get("kind"), p.get("name")): p for p in existing_parts
    }

    # Scan all prefab references in assets/ to enforce AssetExtractor.Seed!
    referenced_guids = set()
    for pfile in assets.rglob("*.prefab"):
        pdoc = prefab.loads(pfile.read_text(encoding="utf-8"))
        for obj in pdoc.objects:
            for comp in obj.components:
                for val in comp.data.values():
                    if isinstance(val, dict) and "guid" in val:
                        referenced_guids.add(val["guid"])

    new_parts = []
    for r in records:
        ag, aname = r["guid"], r["name"]
        glb_bytes = (conv_dir / f"{ag}.glb").read_bytes()
        j_len = struct.unpack_from("<I", glb_bytes, 12)[0]
        gdoc = json.loads(glb_bytes[20:20 + j_len].decode("utf-8"))
        mats = gdoc.get("materials") or []
        for idx, mat in enumerate(mats):
            mname = mat.get("name") or f"material_{idx}"
            prior = by_asset_name.get((ag, "materials", mname))
            if prior is not None:
                mguid, mpath = prior["guid"], prior["path"]
            else:
                mguid = str(uuid.uuid4())
                mpath = f"materials/{aname}.{mname}.material"
                mat_abs = assets / mpath
                mat_abs.parent.mkdir(parents=True, exist_ok=True)
                mat_abs.write_text(f'Name = "{mname}"\\n', encoding="utf-8")
                Path(str(mat_abs) + ".meta").write_text(
                    f'schema_version = 1\\nguid = "{mguid}"\\nimporter = "material"\\n',
                    encoding="utf-8",
                )
            new_parts.append(canonical_toml.InlineTable({
                "kind": "materials", "index": idx, "name": mname,
                "guid": mguid, "path": mpath, "asset": ag,
            }))

        mesh_abs = assets / "meshes" / f"{aname}.mesh"
        if not mesh_abs.exists():
            mguid = str(uuid.uuid4())
            mesh_abs.write_text(
                f'schema_version = 1\\nsource = {{ guid = "{source_guid}", path = "{rel.as_posix()}" }}\\n'
                f'asset = {{ guid = "{ag}", name = "{aname}" }}\\nslot = "mesh"\\n',
                encoding="utf-8",
            )
            Path(str(mesh_abs) + ".meta").write_text(
                f'schema_version = 1\\nguid = "{mguid}"\\nimporter = "mesh"\\n',
                encoding="utf-8",
            )
        else:
            mguid = sidecar.read(str(mesh_abs) + ".meta").guid
        new_parts.append(canonical_toml.InlineTable({
            "kind": "meshes", "index": 0, "name": aname,
            "guid": mguid, "path": f"meshes/{aname}.mesh", "asset": ag,
        }))

        # Enforce Seed: mint a starter prefab UNLESS something already references mguid!
        seed_abs = assets / "prefabs" / f"{aname}.prefab"
        if not seed_abs.exists() and mguid not in referenced_guids:
            seed_abs.parent.mkdir(parents=True, exist_ok=True)
            seed_abs.write_text("schema_version = 1\\n", encoding="utf-8")

    meta_table["extract"] = {"by": "glb", "parts": new_parts}
    meta_path.write_text(canonical_toml.dumps(meta_table), encoding="utf-8")
    sys.exit(0)

if verb == ["assets", "verify"]:
    # Verify every asset has a .meta and every {guid, path} in every .prefab resolves!
    guid_to_rel = {}
    for f in assets.rglob("*"):
        if (
            f.is_dir()
            or f.name == "project.toml"
            or f.name.endswith(".meta")
            or ".blend" in f.name and not f.name.endswith(".blend")
        ):
            continue
        m = sidecar.read(str(f) + ".meta")
        if m is None:
            sys.stderr.write(f"error: missing sidecar for {f}\\n")
            sys.exit(1)
        guid_to_rel[m.guid] = f.relative_to(assets).as_posix()
    for pfile in assets.rglob("*.prefab"):
        pdoc = prefab.loads(pfile.read_text(encoding="utf-8"))
        for obj in pdoc.objects:
            for comp in obj.components:
                for val in comp.data.values():
                    items = val if isinstance(val, list) else [val]
                    for item in items:
                        if isinstance(item, dict) and item:
                            if guid_to_rel.get(item.get("guid")) != item.get("path"):
                                sys.stderr.write(f"error: unresolved ref {item} in {pfile}\\n")
                                sys.exit(1)
    print("verify: 0 error(s), 0 warning(s)")
    sys.exit(0)

sys.stderr.write(f"unexpected cli args: {argv}\\n")
sys.exit(2)
'''


def check(condition: bool, label: str) -> bool:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)
    return condition


def _make_mesh_object(name: str, material_name: str, location=(0.0, 0.0, 0.0)) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)], [], [(0, 1, 2)])
    mat = bpy.data.materials.get(material_name) or bpy.data.materials.new(material_name)
    mesh.materials.append(mat)
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    return obj


def main() -> int:
    paradise_assets.register()
    try:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            assets = root / "assets"
            for sub in ("levels", "meshes", "materials", "prefabs"):
                (assets / sub).mkdir(parents=True)
            (root / ".editor").mkdir()

            cli_script = root / "fake_paradise.py"
            cli_script.write_text(FAKE_CLI, encoding="utf-8")
            os.environ["TEST_BLENDER_BIN"] = bpy.app.binary_path
            os.environ["TEST_REPO_ROOT"] = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            cli_patcher = patch.object(
                host, "resolve_cli_command", return_value=[sys.executable, str(cli_script)]
            )
            cli_patcher.start()

            (assets / "project.toml").write_text(
                'schema_version = 1\nname = "detachint"\n\n'
                '[extract]\nmeshes = "meshes"\nmaterials = "materials"\nprefabs = "prefabs"\n',
                encoding="utf-8",
            )
            (root / ".editor" / "authoring-schema.json").write_text(
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

            # Build the whole-file source .blend with Body_Walls + parented Arcade_Roof + Store_Roof
            bpy.ops.wm.read_factory_settings(use_empty=True)
            body_obj = _make_mesh_object("Body_Walls", "Mat_Wall")
            anchor = bpy.data.objects.new("BuildingAnchor", None)
            anchor.location = (10.0, 20.0, 5.0)
            bpy.context.scene.collection.objects.link(anchor)
            roof_1 = _make_mesh_object("Arcade_Roof", "Mat_Roof1", location=(1.0, 2.0, 3.0))
            roof_1.parent = anchor
            roof_2 = _make_mesh_object("Store_Roof", "Mat_Roof2", location=(-4.0, 0.0, 2.0))
            bpy.context.view_layer.update()
            expected_roof_1_world = roof_1.matrix_world.translation.copy()

            blend_path = assets / "levels" / "L_TWLT.level.blend"
            bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))

            # Initial whole-file metadata, mesh document, materials and two placing level prefabs
            for mname, mguid in (
                ("Mat_Wall", MAT_WALL),
                ("Mat_Roof1", MAT_ROOF_1),
                ("Mat_Roof2", MAT_ROOF_2),
            ):
                mfile = assets / "materials" / f"L_TWLT.level.{mname}.material"
                mfile.write_text(f'Name = "{mname}"\n', encoding="utf-8")
                Path(str(mfile) + ".meta").write_text(
                    f'schema_version = 1\nguid = "{mguid}"\nimporter = "material"\n',
                    encoding="utf-8",
                )

            (assets / "levels" / "L_TWLT.level.blend.meta").write_text(
                f'schema_version = 1\nguid = "{SOURCE_GUID}"\nimporter = "glb"\n\n'
                '[extract]\nby = "glb"\nparts = ['
                f'{{ kind = "materials", index = 0, name = "Mat_Wall", guid = "{MAT_WALL}", '
                f'path = "materials/L_TWLT.level.Mat_Wall.material" }}, '
                f'{{ kind = "materials", index = 1, name = "Mat_Roof1", guid = "{MAT_ROOF_1}", '
                f'path = "materials/L_TWLT.level.Mat_Roof1.material" }}, '
                f'{{ kind = "materials", index = 2, name = "Mat_Roof2", guid = "{MAT_ROOF_2}", '
                f'path = "materials/L_TWLT.level.Mat_Roof2.material" }}, '
                f'{{ kind = "meshes", index = 0, name = "L_TWLT.level", guid = "{BODY_MESH_GUID}", '
                f'path = "meshes/L_TWLT.level.mesh" }}]\n',
                encoding="utf-8",
            )
            (assets / "meshes" / "L_TWLT.level.mesh").write_text(
                f'schema_version = 1\nsource = {{ guid = "{SOURCE_GUID}", '
                f'path = "levels/L_TWLT.level.blend" }}\nslot = "mesh"\n',
                encoding="utf-8",
            )
            (assets / "meshes" / "L_TWLT.level.mesh.meta").write_text(
                f'schema_version = 1\nguid = "{BODY_MESH_GUID}"\nimporter = "mesh"\n',
                encoding="utf-8",
            )

            for level_name, root_guid in (
                ("L_TWLT.level.prefab", LEVEL_A_GUID),
                ("L_TWLT_Second.prefab", LEVEL_B_GUID),
            ):
                lpath = assets / "levels" / level_name
                lpath.write_text(
                    "schema_version = 1\n\n[[objects]]\n\n"
                    f'[[objects.components]]\nid = "{well_known.META_ID}"\ntype = "meta"\n'
                    f'Guid = "{root_guid}"\nName = "L_TWLT.level"\n\n'
                    f'[[objects.components]]\nid = "{STATIC_MESH_ID}"\n'
                    'type = "ShiningPie.Authoring.StaticMesh"\n'
                    f'Mesh = {{ guid = "{BODY_MESH_GUID}", path = "meshes/L_TWLT.level.mesh" }}\n\n'
                    f'[[objects.components]]\nid = "{mesh_slots.MATERIALS_ID}"\n'
                    f'type = "{mesh_slots.MATERIALS_TYPE}"\n'
                    "Slots = ["
                    f'{{ guid = "{MAT_WALL}", path = "materials/L_TWLT.level.Mat_Wall.material" }}, '
                    f'{{ guid = "{MAT_ROOF_1}", path = "materials/L_TWLT.level.Mat_Roof1.material" }}, '
                    f'{{ guid = "{MAT_ROOF_2}", path = "materials/L_TWLT.level.Mat_Roof2.material" }}]\n\n'
                    f'[[objects.components]]\nid = "{well_known.TRANSFORM_ID}"\ntype = "transform"\n'
                    "Position = [6.0, 0.0, 3.0]\nRotation = [0.0, 0.0, 0.0, 1.0]\nScale = [1.0, 1.0, 1.0]\n",
                    encoding="utf-8",
                )
                Path(str(lpath) + ".meta").write_text(
                    f'schema_version = 1\nguid = "{root_guid}"\nimporter = "prefab"\n',
                    encoding="utf-8",
                )

            print("\n== UI & preflight refusals before mutating anything ==")
            bpy.ops.object.select_all(action="DESELECT")
            roof_1.select_set(True)
            bpy.context.view_layer.objects.active = roof_1
            check(
                detach_model.is_document_name("Arcade_Roof")
                and bpy.ops.paradise_assets.detach_models.poll(),
                "detach_models polls True in a saved source .blend with a static mesh selected",
            )

            # Refuse detaching ALL mesh objects (empty remainder)
            for o in (body_obj, roof_1, roof_2):
                o.select_set(True)
            refused = None
            try:
                bpy.ops.paradise_assets.detach_models()
            except RuntimeError as error:
                refused = str(error)
            check(
                refused is not None and "at least one drawable mesh" in refused,
                f"detaching every mesh object is refused ({refused})",
            )

            # Refuse extraction namespace collision before writing anything
            bpy.ops.object.select_all(action="DESELECT")
            roof_1.select_set(True)
            blocker = assets / "materials" / "Arcade_Roof.collision.material"
            blocker.write_text('Name = "x"\n', encoding="utf-8")
            refused = None
            try:
                bpy.ops.paradise_assets.detach_models()
            except RuntimeError as error:
                refused = str(error)
            blocker.unlink()
            check(
                refused is not None and "extraction namespace" in refused,
                f"colliding extraction stem is refused before writing ({refused})",
            )
            check(
                not [c for c in bpy.data.collections if c.asset_data is not None],
                "failed preflight left .blend collections untouched",
            )

            print("\n== detaching 2 roofs updates .blend, keeps identities, fans out, mints no seeds ==")
            bpy.ops.object.select_all(action="DESELECT")
            roof_1.select_set(True)
            roof_2.select_set(True)
            bpy.context.view_layer.objects.active = roof_1
            result = bpy.ops.paradise_assets.detach_models()
            check(result == {"FINISHED"}, f"detach_models succeeded ({result})")

            asset_cols = {
                c.name: c for c in bpy.data.collections if c.library is None and c.asset_data is not None
            }
            check(
                set(asset_cols) == {"L_TWLT.level", "Arcade_Roof", "Store_Roof"},
                f"source .blend has 3 asset-marked collections ({sorted(asset_cols)})",
            )
            bpy.context.view_layer.update()
            roof_pos = tuple(roof_1.matrix_world.translation)
            check(
                (roof_1.matrix_world.translation - expected_roof_1_world).length < 1e-5,
                f"parented roof kept its world position after detaching ({roof_pos})",
            )
            check(
                sidecar.read(str(assets / "meshes" / "L_TWLT.level.mesh.meta")).guid == BODY_MESH_GUID,
                "remainder mesh identity survived unchanged",
            )
            check(
                not list((assets / "prefabs").glob("*.prefab")),
                f"zero seed prefabs were minted ({list((assets / 'prefabs').glob('*.prefab'))})",
            )

            for level_name, root_guid in (
                ("L_TWLT.level.prefab", LEVEL_A_GUID),
                ("L_TWLT_Second.prefab", LEVEL_B_GUID),
            ):
                doc = prefab.loads((assets / "levels" / level_name).read_text(encoding="utf-8"))
                check(len(doc.objects) == 3, f"{level_name} now has body + 2 detached objects")
                body_entry, det_a, det_b = doc.objects
                check(
                    [s.get("guid") for s in body_entry.component(mesh_slots.MATERIALS_ID).data["Slots"]]
                    == [MAT_WALL],
                    f"{level_name} body Slots rebuilt to 1 draw keeping MAT_WALL",
                )
                check(
                    det_a.parent == root_guid
                    and det_b.parent == root_guid
                    and det_a.prefab is None
                    and det_b.prefab is None,
                    f"{level_name} detached objects are plain StaticMesh children under the body",
                )
                check(
                    len(det_a.component(mesh_slots.MATERIALS_ID).data["Slots"]) == 1
                    and len(det_b.component(mesh_slots.MATERIALS_ID).data["Slots"]) == 1,
                    f"{level_name} detached objects carry populated Slots matching their GLBs",
                )

            print("\n== loading the updated level in Blender links all 3 models and saves byte-exact ==")
            layout = project.ProjectLayout(str(root))
            level_a_path = str(assets / "levels" / "L_TWLT.level.prefab")
            before_bytes = Path(level_a_path).read_bytes()
            bpy.ops.wm.read_factory_settings(use_empty=True)
            loaded = load.load_document(
                bpy.context.scene,
                prefab.loads(Path(level_a_path).read_text(encoding="utf-8"), level_a_path),
                level_a_path,
                layout,
            )
            check(
                not loaded.warnings and loaded.meshes == 3,
                f"level loads all 3 models cleanly (meshes={loaded.meshes}, warnings={loaded.warnings})",
            )
            save.save_prefab(bpy.context.scene)
            check(
                Path(level_a_path).read_bytes() == before_bytes,
                "saving the loaded level after detach is byte-exact",
            )
    finally:
        paradise_assets.unregister()

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
