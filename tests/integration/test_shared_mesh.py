"""A placed model -> Edit Shared Mesh -> edited in Blender -> written back INTO the shared GLB.

Runs against a COPY of a real asset project (ShiningPie by default). The edit must reach every
placement, keep the model's materials, textures and extraction record intact, and the project
must still verify and build: the engine cooks the rewritten GLB, and its extracted materials
must still match their source.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_editable_mesh import (
    enable,
    open_fresh,
    place,
    primitive_triangles,
    reload,
    same_points,
    slot_faces,
    world_points,
)

from paradise_assets import watch
from paradise_assets.document import gltf, project, sidecar
from paradise_assets.materialize import save, store
from paradise_assets.play import host

LEVEL = "levels/test.prefab"
#: One node, two textured materials: slot order and the materials array are both visible.
BENCH = "prefabs/models/NCP_Shelter_bench_8cbd278d.prefab"
BENCH_GLB = "models/neon_city_props/NCP_Shelter_bench_8cbd278d.glb"
#: Thirteen nodes, each with its own transform: every part must go back to its own node.
TRUCK = "prefabs/models/HighwayTruck_A.prefab"
TRUCK_GLB = "models/HighwayTruck_A.glb"


def edit_shared(obj) -> dict:
    bpy.context.view_layer.objects.active = obj
    return bpy.ops.paradise_assets.edit_shared_mesh("EXEC_DEFAULT")


def finish(obj) -> dict:
    bpy.context.view_layer.objects.active = obj
    return bpy.ops.paradise_assets.finish_shared_mesh("EXEC_DEFAULT")


def untouched_json(document):
    """Everything of a GLB but its geometry: what editing the geometry must leave alone."""
    return {key: value for key, value in document.items()
            if key not in ("meshes", "accessors", "bufferViews", "buffers", "nodes")}


def local_space(obj, points):
    inverse = obj.matrix_world.inverted()
    return [inverse @ point for point in points]


def run(source, root):
    enable()
    shutil.copytree(Path(source, "assets"), Path(root, "assets"))
    Path(root, ".editor").mkdir()
    shutil.copy2(Path(source, ".editor/authoring-schema.json"), Path(root, ".editor/authoring-schema.json"))
    layout = project.ProjectLayout(root)
    level = layout.resolve(LEVEL)
    open_fresh(level, layout)
    scene = bpy.context.scene

    # -- two placements of one model; one edits it in place ------------------------------------
    bench_glb = layout.resolve(BENCH_GLB)
    first, second = place(layout, BENCH), place(layout, BENCH)
    before = gltf.read_json(bench_glb)
    identity = sidecar.read(sidecar.path_for(bench_glb))
    level_bytes = Path(level).read_bytes()
    bench = store.object_with_guid(scene, first)
    shown = world_points(bench)
    assert edit_shared(bench) == {"FINISHED"}
    bench = store.object_with_guid(scene, first)
    assert bench.type == "MESH" and store.editable_of(bench).shared
    assert slot_faces(bench) == primitive_triangles(bench_glb)
    assert same_points(world_points(bench), shown), "the shared mesh does not stand where the instance did"
    assert Path(level).read_bytes() == level_bytes, "starting to edit rewrote the document"
    assert store.object_with_guid(scene, second).instance_collection is not None
    print("PASS a placement edits the shared model where it stood; the document is untouched")

    # -- an untouched save writes nothing; an edit reaches the GLB and every placement ----------
    glb_bytes = Path(bench_glb).read_bytes()
    save.save_prefab(scene)
    assert Path(bench_glb).read_bytes() == glb_bytes and Path(level).read_bytes() == level_bytes
    print("PASS an untouched save rewrites neither the document nor the shared GLB")

    top = max(vertex.co.z for vertex in bench.data.vertices)
    for vertex in bench.data.vertices:
        if vertex.co.z > top - 1e-4:
            vertex.co.z += 0.5
    edited_local = local_space(bench, world_points(bench))
    result = save.save_prefab(scene)
    assert result.meshes == 1 and Path(bench_glb).read_bytes() != glb_bytes
    assert Path(level).read_bytes() == level_bytes, "a geometry edit rewrote the document"
    after = gltf.read_json(bench_glb)
    assert untouched_json(after) == untouched_json(before), "materials, textures or scenes changed"
    assert [node.get("name") for node in after["nodes"]] == [node.get("name") for node in before["nodes"]]
    assert [p.get("material") for m in after["meshes"] for p in m["primitives"]] == \
        [p.get("material") for m in before["meshes"] for p in m["primitives"]]
    assert sidecar.read(sidecar.path_for(bench_glb)).guid == identity.guid
    other = store.object_with_guid(scene, second)
    assert same_points(local_space(other, world_points(other)), edited_local), \
        "the other placement does not show the edit"
    print("PASS an edit is spliced into the shared GLB: materials, nodes and identity kept, "
          "every placement shows it")

    # -- a reload hands the editing object back; Finish returns an ordinary placement -----------
    kept = bench
    reload(level, layout)
    assert store.object_with_guid(scene, first) == kept
    assert finish(kept) == {"FINISHED"}
    bench = store.object_with_guid(scene, first)
    assert bench.type == "EMPTY" and bench.instance_collection is not None
    assert same_points(local_space(bench, world_points(bench)), edited_local)
    assert Path(level).read_bytes() == level_bytes
    print("PASS a reload keeps the edit going; Finish shows an ordinary placement of the new model")

    # -- refusals: a GLB someone else changed; a second editor of the same model ----------------
    assert edit_shared(bench) == {"FINISHED"}
    bench = store.object_with_guid(scene, first)
    changed = Path(bench_glb).read_bytes()
    Path(bench_glb).write_bytes(glb_bytes)
    bench.data.vertices[0].co.z -= 0.25
    try:
        save.save_prefab(scene)
    except save.SaveError as error:
        assert "changed on disk" in str(error), str(error)
    else:
        raise AssertionError("a save over someone else's change went ahead")
    assert Path(bench_glb).read_bytes() == glb_bytes, "a refused save wrote the GLB"
    reload(level, layout)
    assert store.object_with_guid(scene, first).type == "EMPTY", "a stale edit was handed back"
    Path(bench_glb).write_bytes(changed)
    reload(level, layout)
    assert edit_shared(store.object_with_guid(scene, first)) == {"FINISHED"}
    try:
        edit_shared(store.object_with_guid(scene, second))
    except RuntimeError as error:   # bpy.ops raises what a cancelled operator reported
        assert "already editing" in str(error), str(error)
    else:
        raise AssertionError("a second editor of the same shared model was allowed")
    assert finish(store.object_with_guid(scene, first)) == {"FINISHED"}
    print("PASS a GLB changed on disk refuses the save; one editor per shared model")

    # -- thirteen nodes: each part goes back to its own node ------------------------------------
    truck_glb = layout.resolve(TRUCK_GLB)
    truck_before = gltf.read_json(truck_glb)
    truck_guid = place(layout, TRUCK)
    assert edit_shared(store.object_with_guid(scene, truck_guid)) == {"FINISHED"}
    truck = store.object_with_guid(scene, truck_guid)
    for vertex in truck.data.vertices:
        vertex.co.x *= 1.25
    edited_local = local_space(truck, world_points(truck))
    save.save_prefab(scene)
    truck_after = gltf.read_json(truck_glb)
    assert [n.get("translation") for n in truck_after["nodes"]] == \
        [n.get("translation") for n in truck_before["nodes"]], "node transforms changed"
    assert finish(truck) == {"FINISHED"}
    truck = store.object_with_guid(scene, truck_guid)
    assert same_points(local_space(truck, world_points(truck)), edited_local), \
        "a multi-node model was put back wrongly"
    print("PASS a multi-node model's parts go back to their own nodes")

    # -- the engine side: verify is clean and the level builds -----------------------------------
    watch.stop_all()
    report = host.run_cli(["assets", "verify", "--project", root], root)
    assert report is not None and report.ok, (report.stdout + report.stderr) if report else "no CLI"
    built = host.run_cli(["assets", "build", "--profile", "dev", "--project", root], root)
    assert built is not None and built.ok, (built.stdout + built.stderr)[-3000:] if built else "no CLI"
    print("PASS verify is clean after in-place edits, and the level builds")


if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    source = args[0] if args else os.environ.get("PARADISE_ASSETS_PROJECT", "../ShiningPie")
    schema_dump = Path(source, ".editor/authoring-schema.json")
    if not Path(source, "assets", LEVEL).is_file() or not schema_dump.is_file():
        print("SKIP no asset project with", LEVEL, "at", source)
    else:
        output = os.environ.get("PARADISE_SHARED_OUTPUT")
        manager = contextlib.nullcontext(output) if output else tempfile.TemporaryDirectory()
        try:
            with manager as root:
                Path(root).mkdir(parents=True, exist_ok=True)
                run(source, root)
        finally:
            watch.stop_all()
