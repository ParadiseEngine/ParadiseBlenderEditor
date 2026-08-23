"""Viewport preview of authored model paths, inside Blender.

Builds a two-object prop, exports it as the GLB an art pipeline would hand over, points an
entity at it through ``model_path``, and checks the preview machinery:

* the preview attaches as a CHILD of the entity -- the load-bearing design decision, since
  every export consumer walks entity objects and must stay oblivious to previews;
* nothing the import created survives except the one marked mesh datablock;
* the exported scene document is IDENTICAL with and without the preview loaded (the whole
  point of the feature: looking at the level must not change the level);
* clear removes the child and releases the datablock.

Run with::

    blender --background --factory-startup --python tests/integration/test_model_preview.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

import bpy  # noqa: E402
from mathutils import Matrix  # noqa: E402

import paradise_blender  # noqa: E402
from paradise_blender.export.scene import export_scene  # noqa: E402

DATA_DIR = os.path.join(tempfile.gettempdir(), "paradise_preview_test")
GLB_FIELD = "Models/prop.glb"

failures: list[str] = []


def check(condition: bool, description: str, detail: str = "") -> None:
    if condition:
        print(f"ok   {description}")
    else:
        print(f"FAIL {description}{(' — ' + detail) if detail else ''}")
        failures.append(description)


def build_scene() -> None:
    paradise_blender.register()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.paradise_project.data_dir = DATA_DIR
    scene.paradise_project.scene_name_override = "preview_test"
    scene.paradise_project.export_on_save = False

    # The "art pipeline" asset: two objects, so the preview's join path is exercised too.
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0.5))
    body = bpy.context.active_object
    body.name = "PropBody"
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.3, location=(0, 0, 1.2))
    cap = bpy.context.active_object
    cap.name = "PropCap"
    body.select_set(True)
    cap.select_set(True)
    bpy.context.view_layer.objects.active = body
    glb_path = os.path.join(DATA_DIR, *GLB_FIELD.split("/"))
    os.makedirs(os.path.dirname(glb_path), exist_ok=True)
    bpy.ops.export_scene.gltf(filepath=glb_path, export_format="GLB", use_selection=True)

    # The level: one entity that references the prop instead of carrying geometry.
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(2.0, 0.0, 0.0))
    entity = bpy.context.active_object
    entity.name = "PropEntity"
    entity.paradise.is_entity = True
    # Absolute, because a `//`-relative path cannot resolve in an unsaved test .blend.
    entity.paradise.model_path = glb_path


def main() -> int:
    build_scene()
    entity = bpy.data.objects["PropEntity"]

    # -- baseline export, before any preview exists -----------------------------------
    baseline = export_scene(bpy.context.scene)
    if baseline is None:
        print("FAIL export produced no document")
        return 1

    before_objects = len(bpy.data.objects)
    before_meshes = len(bpy.data.meshes)
    before_materials = len(bpy.data.materials)
    before_images = len(bpy.data.images)
    before_actions = len(bpy.data.actions)

    result = bpy.ops.paradise.load_model_preview()
    check(result == {"FINISHED"}, "load finished")

    children = [c for c in entity.children if c.get("paradise_preview_child")]
    check(len(children) == 1, "one preview child attached", str(len(children)))
    child = children[0] if children else None

    check(child is not None and child.type == "MESH" and len(child.data.vertices) > 0,
          "the preview carries geometry")
    check(child is not None and len(child.data.materials) == 0,
          "preview materials are stripped so the contract's Materials list is untouched")
    check(entity.type == "EMPTY", "the entity itself stays an EMPTY", entity.type)
    check(child is not None and child.parent is entity,
          "the child is parented to the entity, so the entity's transform places it")
    check(
        child is not None and child.matrix_local == Matrix.Identity(4),
        "identity local transform: the child sits where the runtime would render the GLB",
    )
    check(len(bpy.data.objects) == before_objects + 1,
          "only the child object was added",
          f"{len(bpy.data.objects)} vs {before_objects + 1}")
    check(len(bpy.data.meshes) == before_meshes + 1,
          "only the preview mesh datablock was added",
          f"{len(bpy.data.meshes)} vs {before_meshes + 1}")
    check(len(bpy.data.materials) == before_materials and len(bpy.data.images) == before_images
          and len(bpy.data.actions) == before_actions,
          "no materials, images or actions leaked from the import")

    preview_mesh = child.data if child is not None else None
    preview_mesh_name = preview_mesh.name if preview_mesh is not None else ""
    check(preview_mesh is not None and preview_mesh.get("paradise_preview_source") is not None,
          "the datablock is marked with its source path")

    # -- the guarantee that matters: the export did not change -------------------------
    with_preview = export_scene(bpy.context.scene)
    with open(baseline, encoding="utf-8") as handle:
        before = json.load(handle)
    with open(with_preview, encoding="utf-8") as handle:
        after = json.load(handle)
    check(before == after, "the scene document is identical with the preview loaded")
    renderable = next(
        (c["Data"] for e in after["Entities"] if e["Id"] == "PropEntity"
         for c in e["Components"] if c["Type"].endswith("RenderableComponentData")),
        None,
    )
    check(renderable == {"Mesh": GLB_FIELD, "MeshNode": None},
          "the entity references the authored GLB", str(renderable))

    # -- reload is a no-op on an unchanged file -----------------------------------------
    mesh_before = preview_mesh
    bpy.ops.paradise.load_model_preview()
    children = [c for c in entity.children if c.get("paradise_preview_child")]
    check(len(children) == 1 and children[0].data is mesh_before,
          "reloading an unchanged file reuses the datablock")

    # -- clear ----------------------------------------------------------------------------
    result = bpy.ops.paradise.clear_model_preview()
    check(result == {"FINISHED"}, "clear finished")
    check(not [c for c in entity.children if c.get("paradise_preview_child")],
          "clear removed the preview child")
    check(preview_mesh_name not in bpy.data.meshes,
          "clear released the orphaned datablock")
    check(len(bpy.data.objects) == before_objects,
          "the scene is back to its pre-preview object count")

    # -- scene-wide operators -------------------------------------------------------------
    # Nothing is selected here on purpose: the scene operators must not care.
    bpy.ops.object.select_all(action="DESELECT")
    result = bpy.ops.paradise.load_model_previews_scene()
    check(result == {"FINISHED"}, "scene load finishes with no selection")
    children = [c for c in entity.children if c.get("paradise_preview_child")]
    check(len(children) == 1, "scene load re-attached the preview", str(len(children)))

    # A second entity sharing the same GLB: the scene load must cover it too and share the
    # datablock, and refresh (load again) must be a no-op, not a rebuild.
    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(-2.0, 0.0, 0.0))
    twin = bpy.context.active_object
    twin.name = "PropEntityTwin"
    twin.paradise.is_entity = True
    twin.paradise.model_path = entity.paradise.model_path
    shared = children[0].data
    bpy.ops.paradise.load_model_previews_scene()
    twin_kids = [c for c in twin.children if c.get("paradise_preview_child")]
    check(len(twin_kids) == 1 and twin_kids[0].data is shared,
          "scene load covers new entities and shares one datablock per file")
    check(children[0].data is shared, "refreshing an unchanged file did not rebuild the datablock")

    # An asset that crashes the importer with an UNEXPECTED exception type must not abort
    # the scene-wide batch (the review's hardening case): without containment at the call
    # site, the sum() generator abandons every entity after the raising one. The crash is
    # simulated by monkeypatching rather than by feeding Blender garbage bytes, because
    # what a corrupt GLB raises is importer-version-dependent -- ValueError here is
    # deterministic whatever the importer would have done.
    corrupt_path = os.path.join(DATA_DIR, "Models", "corrupt.glb")
    with open(corrupt_path, "wb") as handle:
        handle.write(b"not really a glb")
    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(4.0, 0.0, 0.0))
    broken = bpy.context.active_object
    broken.name = "BrokenEntity"
    broken.paradise.is_entity = True
    broken.paradise.model_path = corrupt_path

    from paradise_blender.authoring import model_preview as preview_module
    real_preview_mesh = preview_module._preview_mesh

    def exploding(path):
        if path == corrupt_path:
            raise ValueError("simulated importer crash on a malformed GLB")
        return real_preview_mesh(path)

    preview_module._preview_mesh = exploding
    try:
        result = bpy.ops.paradise.load_model_previews_scene()
    finally:
        preview_module._preview_mesh = real_preview_mesh

    check(result == {"FINISHED"}, "scene load survives an asset that crashes the importer")
    check(not [c for c in broken.children if c.get("paradise_preview_child")],
          "the crashing entity got no preview")
    check(len([c for c in entity.children if c.get("paradise_preview_child")]) == 1,
          "entities after the crash still loaded their previews")

    # Deleting an entity orphans its preview child as a root object; the scene unload is the
    # only sweep that still recognises it.
    preview_object_name = children[0].name
    bpy.data.objects.remove(twin, do_unlink=True)
    orphan = bpy.data.objects.get(twin_kids[0].name)
    check(orphan is not None and orphan.get("paradise_preview_child"),
          "a deleted entity leaves its preview child behind")
    result = bpy.ops.paradise.clear_model_previews_scene()
    check(result == {"FINISHED"}, "scene unload finished")
    check(not [o for o in bpy.data.objects if o.get("paradise_preview_child")],
          "scene unload removed every preview child, orphaned ones included")
    check(preview_object_name not in bpy.data.objects,
          "the parented preview child went too")
    check(not [m.name for m in bpy.data.meshes if m.get("paradise_preview_source")],
          "scene unload released the shared datablock")

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(f"All checks passed. Documents in {DATA_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
