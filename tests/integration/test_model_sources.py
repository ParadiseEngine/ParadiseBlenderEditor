"""Model sources other than a ``.glb`` -- converted ones (a ``.blend``, an ``.fbx``, an ``.obj``
with its ``.mtl`` and texture, an animation-only ``.bvh``, a ``.blend`` holding two asset
collections) and a ``.gltf`` with its ``.bin`` and texture, which is read as it is -- placed,
shown, and edited at source.

Runs against a COPY of a real asset project (ShiningPie by default) with the real CLI, because the
conversion is the engine's: ``paradise assets extract`` converts each source with a headless
Blender and extracts from that GLB. A placement shows the SOURCE instead -- a ``.blend`` linked,
modifiers live; any other format through the converter's own importer -- and loading a level
converts nothing; the clips still read the converted GLB. The sources are made here, by a second
headless Blender, so nothing about them is checked in. The asset collections of a ``.blend`` get
their GUIDs from the addon's save handler in THAT Blender, which is how an author's do.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from project_session import enable, open_fresh, place, reload, world_points
from warm_project import copy_project, keep_warm

from paradise_assets import clip_ops, context_menu, watch
from paradise_assets.document import (
    asset_guids,
    glb_clips,
    gltf,
    mesh_document,
    model_source,
    project,
    sidecar,
)
from paradise_assets.materialize import store
from paradise_assets.materialize.meshes import ASSET_KEY, ASSET_NAME_KEY, SOURCE_KEY
from paradise_assets.play import host

LEVEL = "levels/test.prefab"
BLEND = "models/model_sources/Crate.blend"
FBX = "models/model_sources/Barrel.fbx"
OBJ = "models/model_sources/Plank.obj"
MTL = "models/model_sources/Plank.mtl"
PNG = "models/model_sources/textures/Plank_wood.png"
BVH = "models/model_sources/Sway.bvh"
LAMP = "models/model_sources/Lamp.gltf"
LAMP_BIN = "models/model_sources/Lamp.bin"
LAMP_PNG = "models/model_sources/textures/Lamp_paint.png"
POSTS = "models/model_sources/Posts.blend"
#: A document of its own for the .bvh placement: no prefab seed exists for an animation-only
#: source, and the build refuses a model reference, so it is removed before verify and build.
CLIPS_LEVEL = "levels/model_sources_clips.prefab"

#: Run by a separate headless Blender. The crate is a 2 x 1 x 0.5 box with a live Bevel
#: modifier and two materials (top red, the rest blue) -- quads and a modifier in the .blend,
#: triangles with the bevel applied in the GLB -- saved showing its scene beside an empty one,
#: "Aside", which a link listing scenes by name would pick instead. The barrel is a cylinder,
#: exported as FBX. The plank is a unit cube exported as OBJ, its material in a .mtl naming a PNG
#: in textures/. Sway is a two-bone armature with one 20-frame clip, exported as BVH -- no mesh at
#: all. The lamp is a unit cube with a textured material, exported as a .gltf beside its .bin and
#: textures/ PNG.
_MAKE_SOURCES = """
import bpy, os, sys
blend, fbx, obj, png, bvh, lamp = sys.argv[sys.argv.index("--") + 1:]
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
bpy.data.scenes.new("Aside")
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

bpy.ops.wm.read_factory_settings(use_empty=True)
paint = bpy.data.images.new("Lamp_paint", 8, 8)
paint.pixels = [0.9, 0.8, 0.1, 1.0] * 64
bpy.ops.mesh.primitive_cube_add(size=1)
cube = bpy.context.active_object
cube.name = "Lamp"
material = bpy.data.materials.new("Paint")
tree = material.node_tree
texture = tree.nodes.new("ShaderNodeTexImage")
texture.image = paint
tree.links.new(texture.outputs["Color"], tree.nodes["Principled BSDF"].inputs["Base Color"])
cube.data.materials.append(material)
bpy.ops.export_scene.gltf(filepath=lamp, export_format="GLTF_SEPARATE", export_texture_dir="textures")
"""

#: Prepended to a script run by a separate headless Blender that saves a .blend of asset
#: collections as an author does: with the addon enabled, whose save handler gives each its GUID.
_WITH_ADDON = """
import addon_utils, bpy, json, sys
sys.path.insert(0, {repository!r})
addon_utils.enable("paradise_assets", default_set=True, persistent=False)
def guids():
    return {{c.name: c.get("paradise_guid") for c in bpy.data.collections if c.asset_data is not None}}
"""

#: One .blend holding two models, each an asset collection laid out beside the other with its
#: origin (``instance_offset``) at its own centre -- a unit cube at x = 10 and one three units
#: tall at x = 20 -- and a monkey in no asset collection, which is no model at all. No Paradise
#: document is open: the save handler gives the collections their GUIDs all the same.
_MAKE_POSTS = """
path = sys.argv[sys.argv.index("--") + 1]
bpy.ops.wm.read_factory_settings(use_empty=True)
addon_utils.enable("paradise_assets", default_set=True, persistent=False)
for name, x, height in (("Post_Short", 10.0, 1.0), ("Post_Tall", 20.0, 3.0)):
    collection = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(collection)
    bpy.ops.mesh.primitive_cube_add(size=1, location=(x, 0.0, 0.0))
    post = bpy.context.active_object
    post.name = name
    post.scale.z = height
    for parent in list(post.users_collection):
        parent.objects.unlink(post)
    collection.objects.link(post)
    collection.instance_offset = (x, 0.0, 0.0)
    collection.asset_mark()
bpy.ops.mesh.primitive_monkey_add(location=(0.0, 0.0, 5.0))
assert set(guids().values()) == {None}
bpy.ops.wm.save_as_mainfile(filepath=path)
"""

#: The author duplicates an asset collection -- Blender copies its custom properties, the GUID
#: with them -- and saves; then deletes the copy and saves again. The copy's name sorts FIRST, so
#: only the project's record can tell which of the two the GUID belongs to.
_DUPLICATE = """
original = bpy.data.collections["Post_Short"]
copy = original.copy()
copy.name = "A_Post_Copy"
bpy.context.scene.collection.children.link(copy)
if copy.asset_data is None:
    copy.asset_mark()
assert copy.get("paradise_guid") == original.get("paradise_guid"), "the copy did not carry the GUID"
bpy.ops.wm.save_mainfile()
saved = guids()
bpy.data.collections.remove(copy)
bpy.ops.wm.save_mainfile()
print("RESULT", json.dumps(saved))
"""

#: The author renames an asset collection and saves.
_RENAME = """
bpy.data.collections["Post_Tall"].name = "Post_Grand"
bpy.ops.wm.save_mainfile()
"""

#: Reads what a .blend's asset collections carry, in a Blender WITHOUT the addon: what was saved.
_READ_GUIDS = """
import bpy, json
print("RESULT", json.dumps({c.name: c.get("paradise_guid") for c in bpy.data.collections
                            if c.asset_data is not None}))
"""

REPOSITORY = str(Path(__file__).resolve().parents[2])

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


def blender(script: str, *args: str, blend: str | None = None, addon: bool = False) -> str:
    """Run ``script`` in a separate headless Blender, with the addon enabled when ``addon``; what
    it printed after ``RESULT``, if anything."""
    if addon:
        script = _WITH_ADDON.format(repository=REPOSITORY) + script
    argv = [bpy.app.binary_path, "--background", "--factory-startup"]
    if blend is not None:
        argv.append(blend)
    argv += ["--python-exit-code", "1", "--python-expr", script, "--", *args]
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
    results = [line[len("RESULT "):] for line in completed.stdout.splitlines() if line.startswith("RESULT ")]
    return results[-1] if results else ""


def saved_guids(blend: str) -> dict:
    """``{asset collection name: paradise_guid}`` as the .blend at ``blend`` holds them."""
    return json.loads(blender(_READ_GUIDS, blend=blend))


def asset_documents(layout, blend: str) -> dict:
    """Every mesh document of an asset of ``blend``: assets-relative path -> its Model."""
    found = {}
    for path in Path(layout.assets).rglob("*.mesh"):
        relative = layout.relative(str(path))
        model = mesh_document.source_for(layout, relative)
        if model is not None and model.asset is not None and os.path.samefile(model.path, blend):
            found[relative] = model
    return found


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


@contextlib.contextmanager
def conversions():
    """The ``paradise assets convert`` runs made inside the block, listed as they happen."""
    real, runs = host.run_cli, []

    def run_cli(arguments, *args, **kwargs):
        if arguments[:2] == ["assets", "convert"]:
            runs.append(arguments)
        return real(arguments, *args, **kwargs)

    with patch.object(host, "run_cli", run_cli):
        yield runs


def file_of(library) -> str:
    return os.path.normcase(os.path.abspath(bpy.path.abspath(library.filepath)))


def has_polygons_beyond_triangles(obj) -> bool:
    """A GLB is triangles only: a quad or n-gon shows the source's own importer made the mesh."""
    return any(len(polygon.vertices) > 3 for polygon in obj.data.polygons)


def placed(scene, guid):
    obj = store.object_with_guid(scene, guid)
    assert obj is not None and obj.instance_collection is not None, "the placement shows no model"
    return obj


def run(source, root):
    enable()
    copy_project(source, root)
    layout = project.ProjectLayout(root)
    level = layout.resolve(LEVEL)
    blend, fbx = layout.resolve(BLEND), layout.resolve(FBX)
    obj, mtl, png, bvh = layout.resolve(OBJ), layout.resolve(MTL), layout.resolve(PNG), layout.resolve(BVH)
    lamp, lamp_bin, lamp_png = layout.resolve(LAMP), layout.resolve(LAMP_BIN), layout.resolve(LAMP_PNG)
    os.makedirs(os.path.dirname(blend))
    blender(_MAKE_SOURCES, blend, fbx, obj, png, bvh, lamp)
    posts = layout.resolve(POSTS)
    blender(_MAKE_POSTS, posts, addon=True)
    minted = saved_guids(posts)
    assert set(minted) == {"Post_Short", "Post_Tall"}, minted
    assert all(asset_guids.canonical_of(guid) == guid for guid in minted.values()), minted
    assert len(set(minted.values())) == 2, f"the two asset collections share a GUID: {minted}"
    short_asset, tall_asset = minted["Post_Short"], minted["Post_Tall"]
    print("PASS saving a .blend with two asset collections gives each a GUID of its own, with no"
          " document open")
    assert Path(mtl).is_file() and "textures/Plank_wood.png" in Path(mtl).read_text(), \
        "no .mtl naming the PNG"
    assert Path(lamp_bin).is_file() and Path(lamp_png).is_file(), "no .bin or texture beside the .gltf"
    sources = (blend, fbx, obj, bvh)

    # The pipeline, not the addon, converts and extracts: identities, the .mesh documents and
    # the model prefab seeds all come from here.
    # A fresh watcher mints the new files' identities before it rebuilds the cold project.
    assert watch.start(root) is None
    for model in (*sources, png, lamp, lamp_png, posts):
        assert sidecar.wait_for(model, timeout=120) is not None, f"the watcher minted no sidecar for {model}"
    watch.stop_all()
    for model in (*sources, lamp, posts):
        cli(["assets", "extract", model], root)
    for model in sources:
        converted = model_source.converted_path(layout, model)
        assert model_source.is_current(model, converted), f"extract left no current conversion of {model}"
    extras = gltf.read_json(model_source.converted_path(layout, obj))["asset"]["extras"]
    recorded = {entry["path"]: entry["sha256"] for entry in extras["paradiseDependencies"]}
    for relative, path in (("Plank.mtl", mtl), ("textures/Plank_wood.png", png)):
        assert recorded.get(relative) == hashlib.sha256(Path(path).read_bytes()).hexdigest(), extras
    print("PASS extraction converts every source into .editor/converted/,"
          " recording the .obj's .mtl and texture")
    assert not Path(model_source.converted_path(layout, lamp)).exists(), "extraction converted the .gltf"
    assert model_source.current_glb(lamp) == lamp
    print("PASS a .gltf is extracted as it is, with no converted GLB")

    # -- a .blend of two asset collections is two models, each converted to a GLB of its own ------
    for name, asset in (("Post_Short", short_asset), ("Post_Tall", tall_asset)):
        converted = model_source.converted_path(layout, posts, asset)
        assert Path(converted).name == f"{asset}.glb", converted
        assert model_source.is_current(posts, converted, asset), f"extract left no current {converted}"
        assert Path(layout.resolve(f"prefabs/models/{name}.prefab")).is_file(), f"no prefab seed for {name}"
    assert not Path(model_source.converted_path(layout, posts)).exists(), "the assets were converted whole"
    assert not Path(layout.resolve("prefabs/models/Posts.prefab")).exists(), "the assets got a whole seed"
    documents = asset_documents(layout, posts)
    assert {(Path(path).stem, model.asset, model.name) for path, model in documents.items()} == {
        ("Post_Short", short_asset, "Post_Short"), ("Post_Tall", tall_asset, "Post_Tall")}, documents
    print("PASS a .blend of two asset collections extracts two models, one converted GLB (named by"
          " its GUID) and prefab seed each, their documents naming the asset by GUID")

    # -- the .bvh gives a skeleton and its clip, and nothing to place as a mesh -----------------
    assert not Path(layout.resolve("prefabs/models/Sway.prefab")).exists(), \
        "an animation-only source got a prefab seed"
    assert not list(Path(layout.resolve("meshes")).glob("Sway*")), \
        "an animation-only source got a mesh document"
    assert list(Path(layout.resolve("animations")).glob("Sway*.skeleton")), "the .bvh extracted no .skeleton"
    assert list(Path(layout.resolve("animations")).glob("Sway*.anim")), "the .bvh extracted no .anim"
    print("PASS a .bvh extracts a skeleton and its clip, and no mesh or prefab")

    with conversions() as runs:
        open_fresh(level, layout)
    scene = bpy.context.scene
    blend_file = os.path.normcase(os.path.abspath(blend))

    # -- a .blend placement links the source: quads, live modifier, its own materials ------------
    with conversions() as more:
        crate_guid = place(layout, seed_prefab(layout, blend))
        reload(level, layout)
    runs += more
    crate = placed(scene, crate_guid)
    assert crate.instance_collection[SOURCE_KEY] == os.path.abspath(blend)
    shown = [obj for obj in crate.instance_collection.all_objects if obj.type == "MESH"]
    assert [obj.library and file_of(obj.library) for obj in shown] == [blend_file], \
        "the placement does not show the object linked from the .blend"
    assert [modifier.type for modifier in shown[0].modifiers] == ["BEVEL"], "the modifier is not live"
    assert len(shown[0].data.vertices) == 8, "the placement shows the bevel applied: a converted GLB"
    materials = [slot.material for slot in shown[0].material_slots]
    assert [material.name for material in materials] == ["Blue", "Red"], materials
    assert all(material.library == shown[0].library for material in materials), \
        "the materials are not the source's own"
    extent = bounds(world_points(crate))
    assert extent == ((3.0, 5.0), (-3.5, -2.5), (-0.25, 0.25)), extent
    print("PASS a .blend placement links its source: modifier live, its own materials, where it was placed")

    # -- an .fbx placement is imported by the converter's own FBX importer ----------------------
    barrel_guid = place(layout, seed_prefab(layout, fbx))
    with conversions() as more:
        reload(level, layout)
    runs += more
    barrel = placed(scene, barrel_guid)
    assert barrel.instance_collection[SOURCE_KEY] == os.path.abspath(fbx)
    parts = [obj for obj in barrel.instance_collection.all_objects if obj.type == "MESH"]
    assert parts and all(obj.library is None and has_polygons_beyond_triangles(obj) for obj in parts), \
        "the .fbx placement shows triangles: the converted GLB, not the FBX importer's mesh"
    extent = bounds(world_points(barrel))
    assert extent == ((3.5, 4.5), (-3.5, -2.5), (-0.5, 0.5)), extent
    print("PASS an .fbx placement is imported natively, as the converter imports it")

    # -- an .obj placement is imported natively, and an edited .mtl re-imports it on reload -----
    watch.stop_all()   # nothing else may convert it: the load must not, either
    plank_guid = place(layout, seed_prefab(layout, obj))
    with conversions() as more:
        reload(level, layout)
    runs += more
    plank = placed(scene, plank_guid)
    assert plank.instance_collection[SOURCE_KEY] == os.path.abspath(obj)
    part = next(part for part in plank.instance_collection.all_objects if part.type == "MESH")
    assert has_polygons_beyond_triangles(part), "the .obj placement shows the converted GLB's triangles"
    tree = part.material_slots[0].material.node_tree
    image = next(node.image for node in tree.nodes if node.type == "TEX_IMAGE")
    assert os.path.normcase(bpy.path.abspath(image.filepath)) == os.path.normcase(png), image.filepath
    extent = bounds(world_points(plank))
    assert extent == ((3.5, 4.5), (-3.5, -2.5), (-0.5, 0.5)), extent

    def roughness(placement):
        part = next(part for part in placement.instance_collection.all_objects if part.type == "MESH")
        nodes = part.material_slots[0].material.node_tree.nodes
        principled = next(node for node in nodes if node.type == "BSDF_PRINCIPLED")
        return principled.inputs["Roughness"].default_value

    before = roughness(plank)
    converted = model_source.converted_path(layout, obj)
    Path(mtl).write_text(Path(mtl).read_text() + "Ns 12.0\n", encoding="utf-8")   # the author retunes it
    with conversions() as more:
        reload(level, layout)
    runs += more
    assert roughness(placed(scene, plank_guid)) != before, "an edited .mtl did not re-import the .obj"
    assert not model_source.is_current(obj, converted), "the reload converted the .obj"
    print("PASS an .obj placement is imported natively with its .mtl and texture, and an edited .mtl"
          " re-imports it on reload")

    # -- a .gltf placement shows the .gltf itself, read with its .bin and texture ---------------
    lamp_guid = place(layout, seed_prefab(layout, lamp))
    with conversions() as more:
        reload(level, layout)
    runs += more
    shown = placed(scene, lamp_guid)
    assert shown.instance_collection[SOURCE_KEY] == os.path.abspath(lamp)
    assert bounds(world_points(shown)) == ((3.5, 4.5), (-3.5, -2.5), (-0.5, 0.5)), bounds(world_points(shown))
    assert any(image.name.startswith("Lamp_paint") for image in bpy.data.images), "the texture was not loaded"
    assert not Path(model_source.converted_path(layout, lamp)).exists(), "showing the .gltf converted it"
    print("PASS a .gltf placement shows the .gltf itself, with its .bin and texture")

    # -- saving the .blend refreshes the placement on reload: its library is reloaded ------------
    watch.stop_all()   # nothing else may convert it: the load must not, either
    stale = model_source.converted_path(layout, blend)
    showing = placed(scene, crate_guid).instance_collection
    blender(_STRETCH, blend=blend)
    with conversions() as more:
        reload(level, layout)
    runs += more
    crate = placed(scene, crate_guid)
    assert crate.instance_collection == showing, "the placement's library collection was replaced"
    assert bounds(world_points(crate))[0] == (2.0, 6.0), bounds(world_points(crate))
    parts = crate.instance_collection.all_objects
    assert [part.library and file_of(part.library) for part in parts] == [blend_file]
    assert [file_of(library) for library in bpy.data.libraries].count(blend_file) == 1, \
        "the .blend was linked twice"
    assert not model_source.is_current(blend, stale), "the reload converted the .blend"
    assert runs == [], f"loading the level ran `paradise assets convert`: {runs}"
    print("PASS a saved .blend is reloaded in place and every placement shows the edit;"
          " no load ran `paradise assets convert`")

    # -- a model is edited at its source: a .blend opens in a new Blender, the rest are refused ---
    # Mesh editing in a level is gone: no registered operator of the addon edits a mesh.
    registered = [name for name in dir(bpy.types) if name.startswith("PARADISE_ASSETS_OT_")]
    assert registered, "the addon registered no operators"
    assert not [name for name in registered if "mesh" in name.lower()], registered
    assert not [name for name in dir(bpy.ops.paradise_assets) if "mesh" in name], dir(bpy.ops.paradise_assets)
    bpy.context.view_layer.objects.active = crate
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
    bpy.context.view_layer.objects.active = placed(scene, lamp_guid)
    try:
        bpy.ops.paradise_assets.edit_model_source("EXEC_DEFAULT")
    except RuntimeError as error:
        assert "GLTF" in str(error) and "export" in str(error), str(error)
    else:
        raise AssertionError("Edit Source opened a .gltf")
    print("PASS no mesh-editing operator is registered; a .blend's Edit Source opens it in a new"
          " Blender; an FBX, OBJ or .gltf is refused, naming its format")

    # -- each asset of a .blend places at its own origin, not where it sits in the file ---------
    short_guid = place(layout, seed_prefab(layout, "Post_Short"))
    tall_guid = place(layout, seed_prefab(layout, "Post_Tall"))
    reload(level, layout)
    short, tall = placed(scene, short_guid), placed(scene, tall_guid)
    posts_file = os.path.normcase(os.path.abspath(posts))
    for shown, asset, name in ((short, short_asset, "Post_Short"), (tall, tall_asset, "Post_Tall")):
        assert shown.instance_collection[SOURCE_KEY] == os.path.abspath(posts)
        assert shown.instance_collection[ASSET_KEY] == asset
        assert [(part.name, part.library and file_of(part.library))
                for part in shown.instance_collection.all_objects] == [(name, posts_file)], \
            f"{name} does not show its collection's objects linked from the .blend"
    assert [file_of(library) for library in bpy.data.libraries].count(posts_file) == 1, \
        "the two assets linked their .blend twice"
    assert short.instance_collection != tall.instance_collection
    assert bounds(world_points(short)) == ((3.5, 4.5), (-3.5, -2.5), (-0.5, 0.5)), bounds(world_points(short))
    assert bounds(world_points(tall)) == ((3.5, 4.5), (-3.5, -2.5), (-1.5, 1.5)), bounds(world_points(tall))
    assert not any(part.name.startswith("Suzanne") for shown in (short, tall)
                   for part in shown.instance_collection.all_objects), "an object outside every asset showed"
    print("PASS the two assets of one .blend are linked as two models, each at its own origin")

    # -- Edit Source on an asset opens its .blend ------------------------------------------------
    bpy.context.view_layer.objects.active = short
    with patch.object(context_menu.subprocess, "Popen") as popen:
        assert bpy.ops.paradise_assets.edit_model_source("EXEC_DEFAULT") == {"FINISHED"}
    assert popen.call_args.args[0] == [bpy.app.binary_path, os.path.abspath(posts)], popen.call_args
    print("PASS Edit Source on an asset of a .blend opens that .blend in a new Blender")

    # -- a duplicated asset collection gets a GUID of its own; the original keeps its own ---------
    saved = json.loads(blender(_DUPLICATE, blend=posts, addon=True))
    assert saved["Post_Short"] == short_asset, f"the original lost its GUID to its copy: {saved}"
    assert saved["Post_Tall"] == tall_asset, saved
    copied = saved["A_Post_Copy"]
    assert asset_guids.canonical_of(copied) == copied and copied not in (short_asset, tall_asset), saved
    assert saved_guids(posts) == minted, "removing the copy changed the other GUIDs"
    print("PASS a duplicated asset collection gets a fresh GUID on save; the one the project records"
          " keeps its own")

    # -- a renamed asset collection keeps its identity: placements, documents, conversion ------------
    blender(_RENAME, blend=posts, addon=True)
    assert saved_guids(posts) == {"Post_Short": short_asset, "Post_Grand": tall_asset}
    reload(level, layout)
    tall = placed(scene, tall_guid)
    assert tall.instance_collection[ASSET_KEY] == tall_asset
    assert tall.instance_collection[ASSET_NAME_KEY] == "Post_Grand", "not the renamed collection"
    assert bounds(world_points(tall))[2] == (-1.5, 1.5), bounds(world_points(tall))
    print("PASS a renamed asset collection still shows at its placements before any re-extract,"
          " linked by its GUID")

    before = set(asset_documents(layout, posts))
    cli(["assets", "extract", posts], root)
    documents = asset_documents(layout, posts)
    assert set(documents) == before, f"the rename renamed or added documents: {sorted(documents)}"
    assert {(Path(path).stem, model.asset, model.name) for path, model in documents.items()} == {
        ("Post_Short", short_asset, "Post_Short"), ("Post_Tall", tall_asset, "Post_Grand")}, documents
    assert model_source.is_current(posts, model_source.converted_path(layout, posts, tall_asset), tall_asset)
    reload(level, layout)
    tall, short = placed(scene, tall_guid), placed(scene, short_guid)
    assert tall.instance_collection[ASSET_KEY] == tall_asset
    assert [part.name for part in tall.instance_collection.all_objects] == ["Post_Tall"]
    assert bounds(world_points(tall))[2] == (-1.5, 1.5) and bounds(world_points(short))[2] == (-0.5, 0.5)
    assert clip_ops.model_for_object(tall, layout) == model_source.Model(posts, tall_asset)
    assert clip_ops.model_for_object(tall, layout).label == "Post_Grand in Posts.blend"
    print("PASS re-extracting a renamed asset keeps its documents and GUIDs, updating only the name"
          " hint; its placements still resolve")

    # -- an animation-only placement loads cleanly: clips authorable, no mesh to edit ------------
    clips_level = layout.resolve(CLIPS_LEVEL)
    Path(clips_level).write_text(_CLIPS_DOCUMENT.format(guid=SWAY_GUID, bvh=BVH), encoding="utf-8")
    open_fresh(clips_level, layout)   # asserts the load warned about nothing
    sway = placed(bpy.context.scene, SWAY_GUID)
    # What it shows is its armature, imported by the converter's BVH importer and posed by its clip.
    rigs = [part for part in sway.instance_collection.all_objects if part.type == "ARMATURE"]
    assert len(rigs) == 1 and rigs[0].animation_data and rigs[0].animation_data.action, rigs
    assert clip_ops.model_for_object(sway, layout) == model_source.Model(bvh)
    view = glb_clips.view(bvh)
    assert view is not None and len(view.clips) == 1 and view.identified, view
    assert view.joints and view.root_joint == "Hips", view
    assert bpy.ops.paradise_assets.clip_root_motion(model=bvh, index=0, enabled=True) == {"FINISHED"}
    assert glb_clips.read_settings(sidecar.path_for(bvh))[0].root_motion is True
    bpy.context.view_layer.objects.active = sway
    try:
        bpy.ops.paradise_assets.edit_model_source("EXEC_DEFAULT")
    except RuntimeError as error:
        assert "no mesh" in str(error), str(error)
    else:
        raise AssertionError("Edit Source ran on an animation-only model")
    watch.stop_all()
    for leftover in (clips_level, sidecar.path_for(clips_level)):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(leftover)
    print("PASS a .bvh placement shows its armature and clip, loads without a warning, its clip is"
          " authorable, and Edit Source is refused")

    # -- the engine side: the project verifies and the level builds -----------------------------
    watch.stop_all()
    report = cli(["assets", "verify"], root)
    assert "has not been extracted" not in report.stdout + report.stderr, report.stdout + report.stderr
    cli(["assets", "build", "--profile", "dev"], root)
    print("PASS verify is clean and the level builds with .blend, .fbx, .obj and .gltf models and"
          " assets of a .blend in it")
    keep_warm(source, root)


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
