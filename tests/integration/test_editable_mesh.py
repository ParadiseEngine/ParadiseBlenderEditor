"""A placed model -> Make Mesh Editable -> edited in Blender -> its own GLB -> reloaded -> built.

Runs against a COPY of a real asset project (ShiningPie by default) with the CLI's watcher
minting the ``.mesh`` documents, because both halves of the contract live outside the addon: the
engine binds ``Slots[i]`` to glTF primitive ``i`` and cooks what the GLB says, and the watcher is
the only thing that may mint identities.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

import bmesh
import bpy
from mathutils import Matrix
from mathutils.kdtree import KDTree

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import addon_utils

from paradise_assets import watch
from paradise_assets.document import editable_mesh as ownership
from paradise_assets.document import gltf, material_document, prefab, project, sidecar
from paradise_assets.materialize import load, save, store
from paradise_assets.play import host

LEVEL = "levels/test.prefab"
CUBE = "prefabs/models/Prim_Cube.prefab"
#: One node, two primitives with different materials: slot order is visible.
BENCH = "prefabs/models/NCP_Shelter_bench_8cbd278d.prefab"
#: Thirteen nodes that all share one name: parts cannot be told apart by name.
TRUCK = "prefabs/models/HighwayTruck_A.prefab"


def enable():
    addon_utils.enable("paradise_assets", default_set=True, persistent=False)
    preferences = bpy.context.preferences.addons["paradise_assets"].preferences
    preferences.auto_watch = False
    preferences.cli = os.environ.get("PARADISE_ASSETS_CLI", "")


def open_fresh(path, layout):
    """Load as a new machine would: nothing of a previous session left to reuse."""
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for mesh in list(bpy.data.meshes):
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    return reload(path, layout)


def reload(path, layout):
    result = load.load_document(bpy.context.scene, prefab.loads(Path(path).read_text(), path), path, layout)
    assert not result.warnings, result.warnings
    return result


def world_points(obj):
    """Every vertex ``obj`` shows, in world space -- an instance's through its library collection."""
    if obj.type == "MESH":
        return [obj.matrix_world @ vertex.co for vertex in obj.data.vertices]
    collection = obj.instance_collection
    base = obj.matrix_world @ Matrix.Translation(-collection.instance_offset)
    return [base @ part.matrix_world @ vertex.co
            for part in collection.all_objects if part.type == "MESH" for vertex in part.data.vertices]


def same_points(a, b, tolerance=1e-4) -> bool:
    """The same point SET: welded or split vertices differ in count, not in where they are."""
    for points, other in ((a, b), (b, a)):
        tree = KDTree(len(other))
        for index, point in enumerate(other):
            tree.insert(point, index)
        tree.balance()
        if any(tree.find(point)[2] > tolerance for point in points):
            return False
    return True


def slot_faces(obj) -> list[int]:
    counts = [0] * len(obj.material_slots)
    for polygon in obj.data.polygons:
        counts[polygon.material_index] += 1
    return counts


def primitive_triangles(path) -> list[int]:
    document = gltf.read_json(path)
    accessors = document["accessors"]
    return [accessors[primitive["indices"]]["count"] // 3
            for _node, mesh, _world in ownership.mesh_instances(document)
            for primitive in document["meshes"][mesh]["primitives"]]


def entry_of(path, guid):
    return prefab.loads(Path(path).read_text(), path).by_guid()[guid]


def owned_glb(layout, path, guid) -> str:
    """The GLB the document now points ``guid``'s mesh at, through its ``.mesh`` document."""
    mesh = next(value["path"] for component in entry_of(path, guid).components
                for value in component.data.values() if isinstance(value, dict)
                and str(value.get("path", "")).endswith(".mesh"))
    with open(layout.resolve(mesh), "rb") as handle:
        return layout.resolve(tomllib.load(handle)["source"]["path"])


def make_editable(obj) -> dict:
    """Run the operator with a FRESH watcher. The copied project has a cold build cache, so a
    watcher that has seen one change is busy rebuilding every asset for minutes and queues new
    files behind that; a fresh one reconciles them first. (A working session's watcher is warm.)"""
    watch.stop_all()
    bpy.context.view_layer.objects.active = obj
    return bpy.ops.paradise_assets.make_mesh_editable("EXEC_DEFAULT")


def place(layout, relative):
    bpy.context.scene.cursor.location = (4, -3, 0)
    assert bpy.ops.paradise_assets.add_prefab_instance(filepath=layout.resolve(relative)) == {"FINISHED"}
    placed = bpy.context.active_object
    save.save_prefab(bpy.context.scene)
    return store.guid_of(placed)


def expect_refusal(level, glb, words):
    before = (Path(level).read_bytes(), Path(glb).read_bytes())
    try:
        save.save_prefab(bpy.context.scene)
    except save.SaveError as error:
        assert words in str(error), str(error)
    else:
        raise AssertionError(f"the save went ahead; expected a refusal naming '{words}'")
    assert (Path(level).read_bytes(), Path(glb).read_bytes()) == before, "a refused save wrote something"


def run(source, root):
    enable()
    shutil.copytree(Path(source, "assets"), Path(root, "assets"))
    Path(root, ".editor").mkdir()
    shutil.copy2(Path(source, ".editor/authoring-schema.json"), Path(root, ".editor/authoring-schema.json"))
    layout = project.ProjectLayout(root)
    level = layout.resolve(LEVEL)
    open_fresh(level, layout)
    scene = bpy.context.scene

    # -- a shared primitive becomes this placement's own mesh ----------------------------------
    cube = next(obj for obj in scene.collection.all_objects if (store.prefab_of(obj) or ("", ""))[1] == CUBE)
    guid, shown = store.guid_of(cube), world_points(cube)
    shared = layout.resolve("models/Prim_Cube.glb")
    shared_bytes = Path(shared).read_bytes()
    assert make_editable(cube) == {"FINISHED"}
    cube = store.object_with_guid(scene, guid)
    assert cube.type == "MESH" and cube.instance_collection is None
    assert same_points(world_points(cube), shown), "the editable mesh does not stand where the instance did"
    assert len(cube.data.vertices) == 8, "the GLB's split corners must weld back to one vertex per corner"
    assert all(len(edge.link_faces) == 2 for edge in cube.data.edges), "every edge of a cube is shared"
    glb = owned_glb(layout, level, guid)
    assert Path(glb).parent == Path(level).with_suffix(""), glb
    assert ownership.owner_of(glb) == guid
    assert "materials" not in gltf.read_json(glb), "an owned GLB carries glTF materials for extract to want"
    assert entry_of(level, guid).prefab is None, "the instance was not unpacked"
    assert Path(shared).read_bytes() == shared_bytes, "the shared model changed"
    others = [obj for obj in scene.collection.all_objects if (store.prefab_of(obj) or ("", ""))[1] == CUBE]
    assert others and all(obj.instance_collection is not None for obj in others)
    print("PASS a placement gets its own mesh where it stood; the shared model and other placements stay")

    # -- saving: nothing when nothing changed, the GLB alone when the geometry did -------------
    level_bytes, glb_bytes = Path(level).read_bytes(), Path(glb).read_bytes()
    save.save_prefab(scene)
    assert (Path(level).read_bytes(), Path(glb).read_bytes()) == (level_bytes, glb_bytes)
    print("PASS an untouched save rewrites neither the document nor the GLB")

    mesh_identity = sidecar.read(sidecar.path_for(glb)).guid
    cube.data.vertices[0].co.z += 0.5
    result = save.save_prefab(scene)
    assert result.meshes == 1 and Path(glb).read_bytes() != glb_bytes
    assert Path(level).read_bytes() == level_bytes, "a geometry edit rewrote the document"
    assert ownership.owner_of(glb) == guid and sidecar.read(sidecar.path_for(glb)).guid == mesh_identity
    edited = world_points(cube)
    print("PASS an edited mesh is written back to its GLB, the document and identities untouched")

    # -- what a GLB cannot hold survives a reload; a fresh load gets what the GLB holds --------
    bm = bmesh.new()
    bm.from_mesh(cube.data)
    bmesh.ops.join_triangles(bm, faces=bm.faces, angle_face_threshold=0.1, angle_shape_threshold=3.2)
    bm.to_mesh(cube.data)
    bm.free()
    cube.data.update()
    save.save_prefab(scene)
    kept = cube
    reload(level, layout)
    cube = store.object_with_guid(scene, guid)
    assert cube == kept and any(len(polygon.vertices) == 4 for polygon in cube.data.polygons)
    print("PASS a reload hands the author's object back, quads and all")

    open_fresh(level, layout)
    cube = store.object_with_guid(scene, guid)
    assert cube.type == "MESH" and all(len(polygon.vertices) == 3 for polygon in cube.data.polygons)
    assert same_points(world_points(cube), edited), "a fresh load does not show what was saved"
    save.save_prefab(scene)
    assert Path(level).read_bytes() == level_bytes
    print("PASS a fresh load rebuilds the saved geometry, triangulated, and saves byte-exact")

    # -- refusals: slots that would misbind, a GLB someone else changed -------------------------
    cube.data.materials.append(bpy.data.materials.new("Stray"))
    expect_refusal(level, glb, "material slots changed")
    cube.data.materials.pop()

    Path(glb).write_bytes(glb_bytes)   # someone else put the first version back
    cube.data.vertices[0].co.z -= 0.25
    expect_refusal(level, glb, "changed on disk")
    reload(level, layout)
    cube = store.object_with_guid(scene, guid)
    assert same_points(world_points(cube), shown), "a reload did not take the GLB as it now is"
    save.save_prefab(scene)
    assert Path(glb).read_bytes() == glb_bytes
    print("PASS slot changes and a GLB changed on disk refuse the save before anything is written")

    # -- two materials in one node: slot i stays primitive i -----------------------------------
    bench_guid = place(layout, BENCH)
    source_glb = layout.resolve("models/neon_city_props/NCP_Shelter_bench_8cbd278d.glb")
    bench = store.object_with_guid(scene, bench_guid)
    shown = world_points(bench)
    assert make_editable(bench) == {"FINISHED"}
    bench = store.object_with_guid(scene, bench_guid)
    assert slot_faces(bench) == primitive_triangles(source_glb)
    # Each slot shows the material its Materials entry binds -- the bench's two differ in colour,
    # so a swapped pair would show here, as it would in the game.
    paths = ownership.material_slots([component.data for component in entry_of(level, bench_guid).components])
    shows = [tuple(round(value, 4) for value in slot.material.diffuse_color) for slot in bench.material_slots]
    binds = [tuple(round(value, 4) for value in material_document.base_colour(layout.resolve(path)))
             for path in paths]
    assert shows == binds and len(set(binds)) == 2, (shows, binds)
    assert same_points(world_points(bench), shown)
    bench_glb = owned_glb(layout, level, bench_guid)
    open_fresh(level, layout)
    bench = store.object_with_guid(scene, bench_guid)
    assert slot_faces(bench) == primitive_triangles(source_glb), "a fresh load merged the slots"
    for polygon in bench.data.polygons:
        polygon.material_index = 0
    expect_refusal(level, bench_glb, "have no faces")
    open_fresh(level, layout)
    print("PASS a two-material model keeps one slot per primitive, in order, across a fresh load")

    # -- thirteen same-named nodes: parts placed by their transforms, not their names ----------
    truck_guid = place(layout, TRUCK)
    truck = store.object_with_guid(scene, truck_guid)
    shown = world_points(truck)
    assert make_editable(truck) == {"FINISHED"}
    truck = store.object_with_guid(scene, truck_guid)
    assert len(truck.material_slots) == len(primitive_triangles(layout.resolve("models/HighwayTruck_A.glb")))
    assert same_points(world_points(truck), shown), "a multi-node model was reassembled wrongly"
    print("PASS a model of same-named nodes is rebuilt where every part stood")

    # -- the engine side: verify is quiet about owned GLBs, and the level builds ---------------
    watch.stop_all()
    report = host.run_cli(["assets", "verify", "--project", root], root)
    assert report is not None and report.ok, (report.stdout + report.stderr) if report else "no CLI"
    assert "has not been extracted" not in report.stdout + report.stderr, report.stdout + report.stderr
    built = host.run_cli(["assets", "build", "--profile", "dev", "--project", root], root)
    assert built is not None and built.ok, (built.stdout + built.stderr)[-3000:] if built else "no CLI"
    print("PASS verify raises nothing about owned GLBs, and the level builds")


if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    source = args[0] if args else os.environ.get("PARADISE_ASSETS_PROJECT", "../ShiningPie")
    schema_dump = Path(source, ".editor/authoring-schema.json")
    if not Path(source, "assets", LEVEL).is_file() or not schema_dump.is_file():
        print("SKIP no asset project with", LEVEL, "at", source)
    else:
        output = os.environ.get("PARADISE_EDITABLE_OUTPUT")
        manager = contextlib.nullcontext(output) if output else tempfile.TemporaryDirectory()
        try:
            with manager as root:
                Path(root).mkdir(parents=True, exist_ok=True)
                run(source, root)
        finally:
            watch.stop_all()
