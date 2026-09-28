"""A Blender session on a copied asset project: the addon enabled, a level loaded, placed into."""

from __future__ import annotations

import os
from pathlib import Path

import addon_utils
import bpy
from mathutils import Matrix

from paradise_assets.document import prefab
from paradise_assets.materialize import load, save, store


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


def place(layout, relative):
    """Place the prefab at assets-relative ``relative`` and save the level; its object's guid."""
    bpy.context.scene.cursor.location = (4, -3, 0)
    assert bpy.ops.paradise_assets.add_prefab_instance(filepath=layout.resolve(relative)) == {"FINISHED"}
    placed = bpy.context.active_object
    save.save_prefab(bpy.context.scene)
    return store.guid_of(placed)


def world_points(obj):
    """Every vertex ``obj`` shows, in world space -- an instance's through its library collection."""
    if obj.type == "MESH":
        return [obj.matrix_world @ vertex.co for vertex in obj.data.vertices]
    collection = obj.instance_collection
    base = obj.matrix_world @ Matrix.Translation(-collection.instance_offset)
    return [base @ part.matrix_world @ vertex.co
            for part in collection.all_objects if part.type == "MESH" for vertex in part.data.vertices]
