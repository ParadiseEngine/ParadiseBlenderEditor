"""Raw meshes -> GLB -> CLI prefab -> a new Blender process -> saved instance -> built level.

All writes stay in a temporary copy of the supplied asset project. PARADISE_GEOMETRY_OUTPUT
keeps that copy for a launcher smoke run; PARADISE_ASSETS_CLI selects an already-built CLI.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from unittest.mock import patch

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import addon_utils

from paradise_assets import watch
from paradise_assets.document import prefab, project, sidecar
from paradise_assets.materialize import load, save, store, workfile
from paradise_assets.play import host


def enable():
    addon_utils.enable("paradise_assets", default_set=True, persistent=False)
    preferences = bpy.context.preferences.addons["paradise_assets"].preferences
    preferences.auto_watch = False
    preferences.cli = os.environ.get("PARADISE_ASSETS_CLI", "")


def open_document(path, layout):
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    result = load.load_document(bpy.context.scene, prefab.loads(Path(path).read_text(), path), path, layout)
    assert not result.warnings, result.warnings


def vertices(objects, offset=Vector()):
    graph = bpy.context.evaluated_depsgraph_get()
    return sorted({tuple(round(v, 4) for v in obj.matrix_world @ vertex.co - offset)
                   for obj in objects if obj.type == "MESH"
                   for vertex in obj.evaluated_get(graph).data.vertices})


def cli(arguments, root):
    result = host.run_cli(["assets", *arguments, "--project", root], root)
    assert result is not None and result.ok, result.stdout + result.stderr if result else "No CLI"
    print("PASS CLI", *arguments)


def reopened(root):
    enable()
    layout = project.ProjectLayout(root)
    target = layout.resolve("prefabs/GeometryProbe.prefab")
    before = Path(target).read_bytes()
    identity = sidecar.read(target + ".meta").guid
    bpy.ops.wm.open_mainfile(filepath=workfile.path_for(layout, target))
    expected = [tuple(value) for value in json.loads(Path(root, "expected-vertices.json").read_text())]
    assert vertices(bpy.data.collections["GLB/GeometryProbe"].objects) == expected
    save.save_prefab(bpy.context.scene)
    assert Path(target).read_bytes() == before
    assert sidecar.read(target + ".meta").guid == identity
    print("PASS fresh Blender loads cached workfile, geometry and stable identity; save is byte-exact")


def run(source, root):
    enable()
    shutil.copytree(Path(source, "assets"), Path(root, "assets"))
    Path(root, ".editor").mkdir()
    shutil.copy2(Path(source, ".editor/authoring-schema.json"), Path(root, ".editor/authoring-schema.json"))
    layout = project.ProjectLayout(root)
    level = layout.resolve("levels/test.prefab")
    open_document(level, layout)
    original_document = Path(level).read_bytes()
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    parent = bpy.data.objects.new("UnselectedParent", None)
    bpy.context.scene.collection.objects.link(parent)
    parent.location, parent.scale = (3, -2, 1), (1, 2, 0.5)
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.object
    cube.parent = parent
    cube.rotation_euler = (0.2, 0.3, 0.4)
    cube.location = (1, 0, 0)
    cube.scale = (-1, 1, 1)
    material = bpy.data.materials.new("ProbeRed")
    material.node_tree.nodes.get("Principled BSDF").inputs["Base Color"].default_value = (0.8, 0.1, 0.2, 1)
    cube.data.materials.append(material)
    bpy.ops.mesh.primitive_plane_add(location=(5, 2, 1))
    plate = bpy.context.object
    plate.modifiers.new("Thickness", "SOLIDIFY").thickness = 0.25
    plate.data.materials.append(material)
    green = bpy.data.materials.new("ProbeGreen")
    green.node_tree.nodes.get("Principled BSDF").inputs["Base Color"].default_value = (0.1, 0.7, 0.2, 1)
    plate.material_slots[0].link = "OBJECT"
    plate.material_slots[0].material = green
    cube.select_set(True)
    bpy.context.view_layer.objects.active = cube
    bpy.context.view_layer.update()
    expected = vertices([cube, plate], cube.matrix_world.translation)
    original_objects = set(bpy.data.objects)
    original_transform = cube.matrix_world.copy()
    target = layout.resolve("prefabs/GeometryProbe.prefab")
    with patch.object(watch, "ensure", return_value="test: watcher unavailable"):
        try:
            bpy.ops.paradise_assets.create_prefab(filepath=target)
            raise AssertionError("creation proceeded without a watcher")
        except RuntimeError as error:
            assert "watcher unavailable" in str(error)
    assert not Path(target).exists() and not Path(target).with_suffix(".glb").exists()
    assert set(bpy.data.objects) == original_objects
    print("PASS unavailable watcher leaves no exported files or temporary Blender objects")

    assert bpy.ops.paradise_assets.create_prefab(filepath=target) == {"FINISHED"}
    assert set(bpy.data.objects) == original_objects
    assert bpy.context.active_object == cube and cube.select_get()
    assert cube.matrix_world == original_transform
    assert Path(level).read_bytes() == original_document
    assert sidecar.read(target + ".meta") is not None
    with open(layout.resolve("materials/GeometryProbe.ProbeRed.material"), "rb") as handle:
        color = tomllib.load(handle)["BaseColorFactor"]
    assert abs(color["r"] - 0.8) < 1e-6 and abs(color["g"] - 0.1) < 1e-6
    with open(layout.resolve("materials/GeometryProbe.ProbeGreen.material"), "rb") as handle:
        color = tomllib.load(handle)["BaseColorFactor"]
    assert abs(color["g"] - 0.7) < 1e-6
    print("PASS creation preserves scene, selection, parent transform and existing document")

    watch.stop_all()
    snapshot = {path: path.read_bytes() for path in Path(root, "assets").rglob("*") if path.is_file()}
    try:
        bpy.ops.paradise_assets.create_prefab(filepath=target)
        raise AssertionError("existing prefab was overwritten")
    except RuntimeError as error:
        assert "already exists" in str(error)
    assert all(path.read_bytes() == content for path, content in snapshot.items())
    print("PASS duplicate creation refuses without altering saved assets")

    open_document(target, layout)
    actual = vertices(bpy.data.collections["GLB/GeometryProbe"].objects)
    assert actual == expected, (actual, expected)
    save.save_prefab(bpy.context.scene)
    workfile.save(layout, target)
    Path(root, "expected-vertices.json").write_text(json.dumps(actual))
    child = subprocess.run([
        bpy.app.binary_path, "--background", "--factory-startup", "--python-exit-code", "1",
        "--python", __file__, "--", "reopen", root,
    ], capture_output=True, text=True, timeout=60)
    assert child.returncode == 0, child.stdout + child.stderr
    print(child.stdout)

    open_document(level, layout)
    bpy.context.scene.cursor.location = (2, 3, 1)
    assert bpy.ops.paradise_assets.add_prefab_instance(filepath=target) == {"FINISHED"}
    placed = bpy.context.active_object
    assert placed.instance_collection is not None
    instance_guid = store.guid_of(placed)
    save.save_prefab(bpy.context.scene)
    before = Path(level).read_bytes()
    open_document(level, layout)
    placed = store.object_with_guid(bpy.context.scene, instance_guid)
    assert placed is not None and placed.instance_collection is not None
    save.save_prefab(bpy.context.scene)
    assert Path(level).read_bytes() == before

    scratch = bpy.data.scenes.new("Raw geometry without a document")
    bpy.context.window.scene = scratch
    bpy.ops.mesh.primitive_cube_add()
    assert store.read_state(scratch) is None
    assert bpy.ops.paradise_assets.create_prefab(
        filepath=layout.resolve("prefabs/WithoutDocument.prefab")) == {"FINISHED"}
    assert store.read_state(scratch) is None and bpy.context.active_object.type == "MESH"
    print("PASS creating from a fresh Blender scene requires no open prefab document")

    watch.stop_all()
    cli(["prefab-check"], root)
    cli(["verify"], root)
    cli(["build", "--profile", "dev"], root)
    print(f"PASS reusable geometry prefab and instance built: {root}/build/levels/test.toml")


if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if args and args[0] == "reopen":
        reopened(args[1])
    else:
        source = args[0] if args else os.environ.get("PARADISE_ASSETS_PROJECT", "../ShiningPie")
        if not Path(source, "assets/project.toml").is_file():
            print("SKIP no asset project", source)
        else:
            output = os.environ.get("PARADISE_GEOMETRY_OUTPUT")
            manager = contextlib.nullcontext(output) if output else tempfile.TemporaryDirectory()
            try:
                with manager as root:
                    Path(root).mkdir(parents=True, exist_ok=True)
                    run(source, root)
            finally:
                watch.stop_all()
