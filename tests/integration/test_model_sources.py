"""Converted model sources -- a ``.blend``, an ``.fbx``, an ``.obj`` with its ``.mtl`` and texture,
and an animation-only ``.bvh`` -- placed, shown, made editable, edited at source.

Runs against a COPY of a real asset project (ShiningPie by default) with the real CLI, because the
conversion is the engine's: ``paradise assets extract`` converts each source with a headless
Blender and extracts from that GLB, and the viewport must show exactly that GLB -- modifiers
applied -- not whatever Blender's own importers would make of the source. The sources are made
here, by a second headless Blender, so nothing about them is checked in.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_editable_mesh import enable, make_editable, open_fresh, owned_glb, place, reload, world_points

from paradise_assets import clip_ops, context_menu, watch
from paradise_assets.document import editable_mesh as ownership
from paradise_assets.document import glb_clips, gltf, model_source, project, sidecar
from paradise_assets.materialize import store
from paradise_assets.materialize.meshes import SOURCE_KEY
from paradise_assets.play import host

LEVEL = "levels/test.prefab"
BLEND = "models/model_sources/Crate.blend"
FBX = "models/model_sources/Barrel.fbx"
OBJ = "models/model_sources/Plank.obj"
MTL = "models/model_sources/Plank.mtl"
PNG = "models/model_sources/textures/Plank_wood.png"
BVH = "models/model_sources/Sway.bvh"
#: A document of its own for the .bvh placement: no prefab seed exists for an animation-only
#: source, and the build refuses a model reference, so it is removed before verify and build.
CLIPS_LEVEL = "levels/model_sources_clips.prefab"

#: Run by a separate headless Blender. The crate is a 2 x 1 x 0.5 box with a live Bevel
#: modifier and two materials (top red, the rest blue) -- quads and a modifier in the .blend,
#: triangles with the bevel applied in the GLB. The barrel is a cylinder, exported as FBX. The
#: plank is a unit cube exported as OBJ, its material in a .mtl naming a PNG in textures/. Sway is
#: a two-bone armature with one 20-frame clip, exported as BVH -- no mesh at all.
_MAKE_SOURCES = """
import bpy, os, sys
blend, fbx, obj, png, bvh = sys.argv[sys.argv.index("--") + 1:]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=2)
crate = bpy.context.active_object
crate.name = "Crate"
crate.scale = (1.0, 0.5, 0.25)
bpy.ops.object.transform_apply(scale=True)
crate.modifiers.new("Bevel", "BEVEL").width = 0.05
for name in ("Blue", "Red"):
    crate.data.materials.append(bpy.data.materials.new(name))
for polygon in crate.data.polygons:
    polygon.material_index = 1 if polygon.normal.z > 0.5 else 0
bpy.ops.wm.save_as_mainfile(filepath=blend)

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cylinder_add(radius=0.5, depth=1.0)
bpy.context.active_object.name = "Barrel"
bpy.ops.export_scene.fbx(filepath=fbx)

bpy.ops.wm.read_factory_settings(use_empty=True)
os.makedirs(os.path.dirname(png))
image = bpy.data.images.new("Plank_wood", 8, 8)
image.pixels = [0.6, 0.4, 0.2, 1.0] * 64
image.filepath_raw = png
image.file_format = "PNG"
image.save()
bpy.ops.mesh.primitive_cube_add(size=1)
plank = bpy.context.active_object
plank.name = "Plank"
material = bpy.data.materials.new("Wood")
tree = material.node_tree
texture = tree.nodes.new("ShaderNodeTexImage")
texture.image = image
tree.links.new(texture.outputs["Color"], tree.nodes["Principled BSDF"].inputs["Base Color"])
plank.data.materials.append(material)
bpy.ops.wm.obj_export(filepath=obj, export_materials=True, path_mode="RELATIVE")

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.object.armature_add()
rig = bpy.context.active_object
bpy.ops.object.mode_set(mode="EDIT")
hips = rig.data.edit_bones[0]
hips.name = "Hips"
spine = rig.data.edit_bones.new("Spine")
spine.head, spine.tail, spine.parent = hips.tail, (0.0, 0.0, 2.0), hips
bpy.ops.object.mode_set(mode="POSE")
for frame, angle in ((1, 0.0), (10, 0.5), (20, 0.0)):
    rig.pose.bones["Spine"].rotation_mode = "XYZ"
    rig.pose.bones["Spine"].rotation_euler.x = angle
    rig.pose.bones["Spine"].keyframe_insert("rotation_euler", frame=frame)
bpy.ops.object.mode_set(mode="OBJECT")
bpy.ops.export_anim.bvh(filepath=bvh, frame_start=1, frame_end=20)
"""

#: A .bvh placement: nothing seeds a prefab for an animation-only source, so an author names it
#: from a skinned mesh component by hand -- the component the game's schema calls a mesh.
_CLIPS_DOCUMENT = """schema_version = 1

[[objects]]

[[objects.components]]
id = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
type = "meta"
Guid = "5b0f3c52-8e0a-4f5e-9d7c-2a6c1f4b9e10"
Name = "model_sources_clips"

[[objects]]

[[objects.components]]
id = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
type = "meta"
Guid = "{guid}"
Name = "Sway"
Parent = "5b0f3c52-8e0a-4f5e-9d7c-2a6c1f4b9e10"

[[objects.components]]
id = "195846ac-d5e5-49a2-8c98-62ac1914c000"
type = "ShiningPie.Authoring.SkinnedMesh"
Mesh = {{ path = "{bvh}" }}
"""
SWAY_GUID = "c7a1e2d4-3b5f-4c6a-8e9d-0f1a2b3c4d5e"

#: Saves the crate twice as long along X -- an author editing the .blend in its own Blender.
_STRETCH = """
import bpy
crate = bpy.data.objects["Crate"]
crate.scale.x = 2.0
bpy.ops.wm.save_mainfile()
"""


def blender(script: str, *args: str, blend: str | None = None) -> None:
    argv = [bpy.app.binary_path, "--background", "--factory-startup"]
    if blend is not None:
        argv.append(blend)
    argv += ["--python-exit-code", "1", "--python-expr", script, "--", *args]
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]


def cli(arguments, root):
    result = host.run_cli([*arguments, "--project", root], root)
    assert result is not None and result.ok, (result.stdout + result.stderr)[-3000:] if result else "no CLI"
    return result


def seed_prefab(layout, source) -> str:
    """The model prefab seed extraction made for ``source``: ``[extract] prefabs`` in ShiningPie
    routes it to ``prefabs/models/<stem>.prefab``."""
    relative = f"prefabs/models/{Path(source).stem}.prefab"
    assert Path(layout.resolve(relative)).is_file(), f"extraction seeded no {relative}"
    return relative


def bounds(points):
    return tuple(
        (round(min(point[axis] for point in points), 3), round(max(point[axis] for point in points), 3))
        for axis in range(3))


def placed(scene, guid):
    obj = store.object_with_guid(scene, guid)
    assert obj is not None and obj.instance_collection is not None, "the placement shows no model"
    return obj


def run(source, root):
    enable()
    shutil.copytree(Path(source, "assets"), Path(root, "assets"))
    Path(root, ".editor").mkdir()
    shutil.copy2(Path(source, ".editor/authoring-schema.json"), Path(root, ".editor/authoring-schema.json"))
    layout = project.ProjectLayout(root)
    level = layout.resolve(LEVEL)
    blend, fbx = layout.resolve(BLEND), layout.resolve(FBX)
    obj, mtl, png, bvh = layout.resolve(OBJ), layout.resolve(MTL), layout.resolve(PNG), layout.resolve(BVH)
    os.makedirs(os.path.dirname(blend))
    blender(_MAKE_SOURCES, blend, fbx, obj, png, bvh)
    assert Path(mtl).is_file() and "textures/Plank_wood.png" in Path(mtl).read_text(), "no .mtl naming the PNG"
    sources = (blend, fbx, obj, bvh)

    # The pipeline, not the addon, converts and extracts: identities, the .mesh documents and
    # the model prefab seeds all come from here.
    # A fresh watcher mints the new files' identities before it rebuilds the cold project.
    assert watch.start(root) is None
    for model in (*sources, png):
        assert sidecar.wait_for(model, timeout=120) is not None, f"the watcher minted no sidecar for {model}"
    watch.stop_all()
    for model in sources:
        cli(["assets", "extract", model], root)
    for model in sources:
        converted = model_source.converted_path(layout, model)
        assert model_source.is_current(model, converted), f"extract left no current conversion of {model}"
    extras = gltf.read_json(model_source.converted_path(layout, obj))["asset"]["extras"]
    recorded = {entry["path"]: entry["sha256"] for entry in extras["paradiseDependencies"]}
    for relative, path in (("Plank.mtl", mtl), ("textures/Plank_wood.png", png)):
        assert recorded.get(relative) == hashlib.sha256(Path(path).read_bytes()).hexdigest(), extras
    print("PASS extraction converts every source into .editor/converted/, recording the .obj's .mtl and texture")

    # -- the .bvh gives a skeleton and its clip, and nothing to place as a mesh -----------------
    assert not Path(layout.resolve("prefabs/models/Sway.prefab")).exists(), "an animation-only source got a prefab seed"
    assert not list(Path(layout.resolve("meshes")).glob("Sway*")), "an animation-only source got a mesh document"
    assert list(Path(layout.resolve("animations")).glob("Sway*.skeleton")), "the .bvh extracted no .skeleton"
    assert list(Path(layout.resolve("animations")).glob("Sway*.anim")), "the .bvh extracted no .anim"
    print("PASS a .bvh extracts a skeleton and its clip, and no mesh or prefab")

    open_fresh(level, layout)
    scene = bpy.context.scene

    # -- a .blend placement shows the converted GLB: bevel applied, where it was placed ----------
    crate_guid = place(layout, seed_prefab(layout, blend))
    reload(level, layout)
    crate = placed(scene, crate_guid)
    assert crate.instance_collection[SOURCE_KEY] == os.path.abspath(blend)
    shown = [obj for obj in crate.instance_collection.all_objects if obj.type == "MESH"]
    assert sum(len(obj.data.vertices) for obj in shown) > 8, "the bevel modifier was not applied"
    extent = bounds(world_points(crate))
    assert extent == ((3.0, 5.0), (-3.5, -2.5), (-0.25, 0.25)), extent
    print("PASS a .blend placement shows its converted GLB, modifiers applied, where it was placed")

    # -- an .fbx placement shows too --------------------------------------------------------------
    barrel_guid = place(layout, seed_prefab(layout, fbx))
    reload(level, layout)
    barrel = placed(scene, barrel_guid)
    assert barrel.instance_collection[SOURCE_KEY] == os.path.abspath(fbx)
    extent = bounds(world_points(barrel))
    assert extent == ((3.5, 4.5), (-3.5, -2.5), (-0.5, 0.5)), extent
    print("PASS an .fbx placement shows its converted GLB")

    # -- an .obj placement shows too, and editing its .mtl reconverts it on reload -------------
    watch.stop_all()   # nothing else may convert it: this is the load's own `assets convert`
    plank_guid = place(layout, seed_prefab(layout, obj))
    reload(level, layout)
    plank = placed(scene, plank_guid)
    assert plank.instance_collection[SOURCE_KEY] == os.path.abspath(obj)
    extent = bounds(world_points(plank))
    assert extent == ((3.5, 4.5), (-3.5, -2.5), (-0.5, 0.5)), extent
    converted = model_source.converted_path(layout, obj)
    Path(mtl).write_text(Path(mtl).read_text() + "Ns 12.0\n", encoding="utf-8")   # the author retunes it
    assert not model_source.is_current(obj, converted), "an edited .mtl left the conversion current"
    reload(level, layout)
    assert model_source.is_current(obj, converted), "reload did not convert the .obj again"
    recorded = {entry["path"]: entry["sha256"]
                for entry in gltf.read_json(converted)["asset"]["extras"]["paradiseDependencies"]}
    assert recorded["Plank.mtl"] == hashlib.sha256(Path(mtl).read_bytes()).hexdigest(), recorded
    placed(scene, plank_guid)
    print("PASS an .obj placement shows its converted GLB, and an edited .mtl is converted again on reload")

    # -- saving the .blend refreshes the placement on reload: the addon converts it itself ------
    watch.stop_all()   # nothing else may convert it: this is the load's own `assets convert`
    stale = model_source.converted_path(layout, blend)
    blender(_STRETCH, blend=blend)
    assert not model_source.is_current(blend, stale)
    reload(level, layout)
    crate = placed(scene, crate_guid)
    assert bounds(world_points(crate))[0] == (2.0, 6.0), bounds(world_points(crate))
    assert model_source.is_current(blend, stale)
    recorded = gltf.read_json(stale)["asset"]["extras"]["paradiseSourceSha256"]
    assert recorded == hashlib.sha256(Path(blend).read_bytes()).hexdigest()
    print("PASS a saved .blend is converted again on reload and every placement shows the edit")

    # -- Edit Shared Mesh is refused for converted models; a .blend opens in a new Blender ------
    bpy.context.view_layer.objects.active = crate
    try:
        bpy.ops.paradise_assets.edit_shared_mesh("EXEC_DEFAULT")
    except RuntimeError as error:
        assert "Edit Source in New Blender" in str(error), str(error)
    else:
        raise AssertionError("Edit Shared Mesh ran on a model converted from a .blend")
    with patch.object(context_menu.subprocess, "Popen") as popen:
        assert bpy.ops.paradise_assets.edit_model_source("EXEC_DEFAULT") == {"FINISHED"}
    assert popen.call_args.args[0] == [bpy.app.binary_path, os.path.abspath(blend)], popen.call_args

    bpy.context.view_layer.objects.active = placed(scene, barrel_guid)
    try:
        bpy.ops.paradise_assets.edit_model_source("EXEC_DEFAULT")
    except RuntimeError as error:
        assert "FBX" in str(error) and "export" in str(error), str(error)
    else:
        raise AssertionError("Edit Source opened an FBX")
    bpy.context.view_layer.objects.active = placed(scene, plank_guid)
    try:
        bpy.ops.paradise_assets.edit_model_source("EXEC_DEFAULT")
    except RuntimeError as error:
        assert "OBJ" in str(error) and "export" in str(error), str(error)
    else:
        raise AssertionError("Edit Source opened an OBJ")
    print("PASS a .blend's Edit Source opens it in a new Blender; an FBX or OBJ is refused, naming its format")

    # -- Make Mesh Editable copies the converted GLB: slot i is its primitive i -----------------
    converted = model_source.converted_path(layout, blend)
    primitives = sum(len(mesh["primitives"]) for mesh in gltf.read_json(converted)["meshes"])
    assert primitives == 2, primitives
    shown = world_points(crate)
    assert make_editable(crate) == {"FINISHED"}
    crate = store.object_with_guid(scene, crate_guid)
    assert crate.type == "MESH" and len(crate.material_slots) == primitives
    assert bounds(world_points(crate)) == bounds(shown), "the editable mesh moved off the placement"
    assert ownership.owner_of(owned_glb(layout, level, crate_guid)) == crate_guid
    print("PASS Make Mesh Editable on a .blend placement copies its converted GLB, one slot per primitive")

    assert make_editable(store.object_with_guid(scene, barrel_guid)) == {"FINISHED"}
    barrel = store.object_with_guid(scene, barrel_guid)
    assert barrel.type == "MESH" and len(barrel.data.polygons) > 0
    print("PASS Make Mesh Editable works on an .fbx placement")

    assert make_editable(store.object_with_guid(scene, plank_guid)) == {"FINISHED"}
    plank = store.object_with_guid(scene, plank_guid)
    assert plank.type == "MESH" and len(plank.material_slots) == 1
    print("PASS Make Mesh Editable works on an .obj placement")

    # -- an animation-only placement loads cleanly: clips authorable, no mesh to edit ------------
    clips_level = layout.resolve(CLIPS_LEVEL)
    Path(clips_level).write_text(_CLIPS_DOCUMENT.format(guid=SWAY_GUID, bvh=BVH), encoding="utf-8")
    open_fresh(clips_level, layout)   # asserts the load warned about nothing
    sway = placed(bpy.context.scene, SWAY_GUID)
    assert any(part.type == "ARMATURE" for part in sway.instance_collection.all_objects)
    assert clip_ops.model_for_object(sway, layout) == bvh
    view = glb_clips.view(bvh)
    assert view is not None and len(view.clips) == 1 and view.identified, view
    assert view.joints and view.root_joint == "Hips", view
    assert bpy.ops.paradise_assets.clip_root_motion(model=bvh, index=0, enabled=True) == {"FINISHED"}
    assert glb_clips.read_settings(sidecar.path_for(bvh))[0].root_motion is True
    bpy.context.view_layer.objects.active = sway
    for operator in (bpy.ops.paradise_assets.make_mesh_editable, bpy.ops.paradise_assets.edit_shared_mesh,
                     bpy.ops.paradise_assets.edit_model_source):
        try:
            operator("EXEC_DEFAULT")
        except RuntimeError as error:
            assert "no mesh" in str(error), str(error)
        else:
            raise AssertionError(f"{operator.idname()} ran on an animation-only model")
    watch.stop_all()
    for leftover in (clips_level, sidecar.path_for(clips_level)):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(leftover)
    print("PASS a .bvh placement loads without a warning, its clip is authorable, and mesh editing is refused")

    # -- the engine side: the project verifies and the level builds -----------------------------
    watch.stop_all()
    report = cli(["assets", "verify"], root)
    assert "has not been extracted" not in report.stdout + report.stderr, report.stdout + report.stderr
    cli(["assets", "build", "--profile", "dev"], root)
    print("PASS verify is clean and the level builds with .blend, .fbx and .obj models in it")


if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    source = args[0] if args else os.environ.get("PARADISE_ASSETS_PROJECT", "../ShiningPie")
    schema_dump = Path(source, ".editor/authoring-schema.json")
    if not Path(source, "assets", LEVEL).is_file() or not schema_dump.is_file():
        print("SKIP no asset project with", LEVEL, "at", source)
    else:
        output = os.environ.get("PARADISE_MODEL_SOURCES_OUTPUT")
        manager = contextlib.nullcontext(output) if output else tempfile.TemporaryDirectory()
        try:
            with manager as root:
                Path(root).mkdir(parents=True, exist_ok=True)
                run(source, root)
        finally:
            watch.stop_all()
