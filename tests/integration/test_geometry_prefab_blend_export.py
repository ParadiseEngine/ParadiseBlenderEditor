"""Native geometry snapshots preserve the live scene and survive relocation out of staging."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paradise_assets.document.new_prefab import CreateError
from paradise_assets.materialize import geometry


def state():
    """The source data and context that a detached snapshot must not change."""
    return {
        "context": (bpy.data.filepath, bpy.data.is_dirty, bpy.context.scene.as_pointer(),
                    bpy.context.mode, bpy.context.scene.frame_current,
                    bpy.context.scene.frame_subframe, bpy.context.active_object.as_pointer()),
        "selected": {obj.as_pointer() for obj in bpy.context.selected_objects},
        "ids": {kind: {(item.as_pointer(), item.name, item.users, item.use_fake_user)
                       for item in getattr(bpy.data, kind)}
                for kind in ("scenes", "collections", "objects", "meshes", "materials", "images")},
        "objects": {obj.as_pointer(): (
            obj.parent, tuple(tuple(row) for row in obj.matrix_world), obj.data,
            tuple((slot.link, slot.material) for slot in obj.material_slots),
            tuple((mod.name, mod.type) for mod in obj.modifiers),
        ) for obj in bpy.data.objects},
        "meshes": {mesh.as_pointer(): (
            tuple(tuple(vertex.co) for vertex in mesh.vertices),
            tuple((tuple(face.vertices), face.material_index) for face in mesh.polygons),
        ) for mesh in bpy.data.meshes},
        "images": {image.as_pointer(): image.filepath for image in bpy.data.images},
    }


def expected_geometry(members, origin):
    graph = bpy.context.evaluated_depsgraph_get()
    expected = {}
    for obj in members:
        evaluated = obj.evaluated_get(graph)
        transform = Matrix.Translation(-origin) @ obj.matrix_world
        normals = transform.to_3x3().inverted().transposed()
        expected[obj.name] = {
            "vertices": [list(transform @ vertex.co) for vertex in evaluated.data.vertices],
            "normals": [list((normals @ face.normal).normalized()) for face in evaluated.data.polygons],
            "materials": [slot.material.name if slot.material else None for slot in evaluated.material_slots],
            "indices": [face.material_index for face in evaluated.data.polygons],
        }
    return expected


def reopened(path, manifest):
    expected = json.loads(Path(manifest).read_text())
    bpy.ops.wm.open_mainfile(filepath=path)
    assert len(bpy.data.scenes) == 1
    assert len(bpy.context.scene.objects) == len(expected["objects"])
    assert set(bpy.data.objects) == set(bpy.context.scene.objects)
    for prefix, source in expected["objects"].items():
        matches = [obj for obj in bpy.context.scene.objects
                   if obj.name == prefix or obj.name.startswith(prefix + ".")]
        assert len(matches) == 1, (prefix, matches)
        obj = matches[0]
        assert obj.type == "MESH" and obj.parent is None and not obj.modifiers
        assert obj.matrix_world == Matrix.Identity(4)
        assert len(obj.data.vertices) == len(source["vertices"])
        for vertex, point in zip(obj.data.vertices, source["vertices"], strict=True):
            assert (vertex.co - Vector(point)).length < 1e-5, (obj.name, vertex.co, point)
        assert len(obj.data.polygons) == len(source["normals"])
        for face, normal in zip(obj.data.polygons, source["normals"], strict=True):
            assert face.normal.dot(Vector(normal)) > 0.9999, (obj.name, face.normal, normal)
        assert [face.material_index for face in obj.data.polygons] == source["indices"]
        materials = [slot.material.name if slot.material else None for slot in obj.material_slots]
        assert materials == source["materials"], (obj.name, materials, source["materials"])
    image = bpy.data.images["SnapshotTexture"]
    assert os.path.isabs(image.filepath), image.filepath
    assert Path(image.filepath).resolve() == Path(expected["texture"]).resolve()
    assert image.packed_file is None
    image.reload()
    assert tuple(image.size) == (2, 2)
    assert abs(image.pixels[0] - 1.0) < 1e-5
    print("PASS reopened native snapshot: baked geometry, winding, materials and relocated texture")


def run(root):
    source_dir, staged_dir = root / "source", root / "staging"
    published_dir = root / "published/assets/prefabs"
    for directory in (source_dir / "textures", staged_dir, published_dir):
        directory.mkdir(parents=True)
    texture = source_dir / "textures/pixel.png"
    pixels = bpy.data.images.new("TexturePixels", width=2, height=2)
    pixels.pixels[:] = [1.0, 0.0, 0.0, 1.0] * 4
    pixels.filepath_raw, pixels.file_format = str(texture), "PNG"
    pixels.save()
    bpy.data.images.remove(pixels)
    image = bpy.data.images.load(str(texture))
    image.name = "SnapshotTexture"
    red = bpy.data.materials.new("SnapshotRed")
    red.use_nodes = True
    node = red.node_tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    red.node_tree.links.new(
        node.outputs["Color"], red.node_tree.nodes.get("Principled BSDF").inputs["Base Color"])
    green = bpy.data.materials.new("SnapshotGreen")
    green.use_nodes = True
    green.node_tree.nodes.get("Principled BSDF").inputs["Base Color"].default_value = (0.1, 0.7, 0.2, 1)

    parent = bpy.data.objects.new("UnselectedParent", None)
    bpy.context.scene.collection.objects.link(parent)
    parent.location, parent.scale = (3, -2, 1), (1, 2, 0.5)
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.object
    cube.name = "SnapshotMirrored"
    cube.parent = parent
    cube.location, cube.rotation_euler, cube.scale = (1, 0, 0), (0.2, 0.3, 0.4), (-1, 1, 1)
    cube.data.materials.append(red)
    twin = bpy.data.objects.new("SnapshotOverride", cube.data)
    bpy.context.scene.collection.objects.link(twin)
    twin.location = (5, 2, 1)
    twin.material_slots[0].link = "OBJECT"
    twin.material_slots[0].material = green
    bpy.ops.mesh.primitive_plane_add(location=(5, -2, 3))
    plate = bpy.context.object
    plate.name = "SnapshotSolidified"
    plate.modifiers.new("Thickness", "SOLIDIFY").thickness = 0.25
    plate.data.materials.append(red)
    members = [cube, twin, plate]
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    for obj in members:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = cube
    bpy.context.scene.frame_set(17)
    bpy.context.view_layer.update()
    source = source_dir / "author.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(source))
    image.filepath = "//textures/pixel.png"
    source_bytes = source.read_bytes()
    saves = []

    def saving(*_args):
        saves.append(True)

    bpy.app.handlers.save_pre.append(saving)
    try:
        for label, active, explicit_origin in (
            ("active", cube, None),
            ("mixed", parent, parent.matrix_world.translation.copy()),
            ("fallback", parent, None),
        ):
            bpy.context.view_layer.objects.active = active
            origin = explicit_origin if explicit_origin is not None else cube.matrix_world.translation
            expected = {"objects": expected_geometry(members, origin), "texture": str(texture)}
            before = state()
            staged = staged_dir / f"{label}.blend"
            geometry.export(bpy.context, members, str(staged), origin=explicit_origin)
            assert state() == before
            assert not saves, "snapshot export invoked document-save handlers"
            assert source.read_bytes() == source_bytes
            assert image.filepath == "//textures/pixel.png"
            assert staged.read_bytes().startswith(b"BLENDER"), "snapshot is not a native .blend"
            assert not Path(str(staged) + ".meta").exists(), "snapshot export minted asset identity"
            published = published_dir / staged.name
            staged.replace(published)
            manifest = published.with_suffix(".json")
            manifest.write_text(json.dumps(expected))
            child = subprocess.run([
                bpy.app.binary_path, "--background", "--factory-startup", "--python-exit-code", "1",
                "--python", __file__, "--", "reopen", str(published), str(manifest),
            ], capture_output=True, text=True, timeout=60)
            assert child.returncode == 0, child.stdout + child.stderr
            print(child.stdout)
        print("PASS active, explicit mixed-selection and fallback origins leave source state untouched")

        empty_mesh = bpy.data.meshes.new("NoFaces")
        empty = bpy.data.objects.new("NoFaces", empty_mesh)
        bpy.context.scene.collection.objects.link(empty)
        before = state()
        try:
            geometry.export(bpy.context, [cube, empty], str(staged_dir / "empty.blend"))
        except CreateError as error:
            assert "no faces" in str(error)
        else:
            raise AssertionError("snapshot accepted a mesh without faces")
        assert state() == before
        assert not (staged_dir / "empty.blend").exists()
        before = state()
        try:
            geometry.export(bpy.context, members, str(root / "missing/output.blend"))
        except (OSError, RuntimeError):
            pass
        else:
            raise AssertionError("snapshot succeeded without an output directory")
        assert state() == before
        assert not saves
        assert source.read_bytes() == source_bytes
        print("PASS invalid geometry and failed writes clean up temporary datablocks")
    finally:
        bpy.app.handlers.save_pre.remove(saving)


if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if args and args[0] == "reopen":
        reopened(args[1], args[2])
    else:
        with tempfile.TemporaryDirectory() as directory:
            run(Path(directory))
