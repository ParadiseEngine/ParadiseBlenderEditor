"""The navmesh bake button and its viewport preview.

The bake normally runs invisibly inside a scene export, which makes tuning walkable geometry
blind: you move a wall, export, and have no idea what the doorway cut actually looks like until
the game runs. The ``paradise.bake_navmesh`` operator runs the same bake on demand and rebuilds
a wireframe overlay from the triangulation the bridge ACTUALLY wrote (via ``--debug-json``) —
not from the input geometry, so erosion, doorway cuts, and dropped slivers are all visible.

The preview object is deliberately inert:

* not an entity and owns no colliders, so both the scene export and the bake itself ignore it;
* unselectable (``hide_select``), so it cannot be grabbed, re-parented, or accidentally marked
  as an entity — delete or rebuild it from the panel instead;
* wireframe drawn in front, because the walkable surface sits a cell (0.1) above the floor and
  would otherwise z-fight or vanish inside the very rooms it is meant to explain.

Visibility is a per-scene setting (``paradise_project.navmesh_preview``) so it survives a
.blend reload with the scene, like every other project-scoped switch.
"""

from __future__ import annotations

import json
import os
import tempfile

import bpy
from bpy.types import Operator

from .. import log
from ..contract import axes
from ..prefs import export_paths
from .navmesh import bake_navmesh
from .scene import resolve_scene_name

__all__ = ["classes", "find_preview_object", "sync_preview_visibility"]

PREVIEW_OBJECT_NAME = "Paradise NavMesh Preview"
PREVIEW_MARKER = "paradise_navmesh_preview"


def find_preview_object() -> bpy.types.Object | None:
    # By marker, not by name: renaming the object must not orphan it into the export.
    return next((o for o in bpy.data.objects if PREVIEW_MARKER in o), None)


def sync_preview_visibility(scene: bpy.types.Scene) -> None:
    """Apply the scene's toggle to the preview object, if one has been baked."""
    preview = find_preview_object()
    if preview is not None:
        preview.hide_viewport = not scene.paradise_project.navmesh_preview


class PARADISE_OT_bake_navmesh(Operator):
    """Bake the navmesh now and rebuild the viewport preview from the result"""

    bl_idname = "paradise.bake_navmesh"
    bl_label = "Bake NavMesh"
    bl_options = {"REGISTER"}

    def execute(self, context):
        scene = context.scene
        scene_name = resolve_scene_name(scene)
        debug_json = os.path.join(
            tempfile.gettempdir(), f"paradise_navmesh_preview_{scene_name}.json"
        )

        try:
            output = bake_navmesh(scene, scene_name, export_paths(scene), debug_json)
            if output is None:
                # bake_navmesh already logged the specific reason to the console; the operator
                # repeats a summary through report() so it reaches the status bar.
                log.warn(
                    "NavMesh bake produced nothing — no walkable geometry, or the bridge is "
                    "not configured (see addon preferences).",
                    self,
                )
                return {"CANCELLED"}

            with open(debug_json, encoding="utf-8") as handle:
                triangulation = json.load(handle)
        finally:
            if os.path.exists(debug_json):
                os.unlink(debug_json)

        vertices = triangulation.get("vertices") or []
        triangles = triangulation.get("triangles") or []
        _rebuild_preview(scene, vertices, triangles)

        # Baking is an explicit request to look at the result: flip the toggle on rather than
        # leaving a freshly baked preview invisible behind a switch the author forgot about.
        scene.paradise_project.navmesh_preview = True
        sync_preview_visibility(scene)

        log.info(
            f"Baked navmesh: {len(triangles) // 3} triangles -> {os.path.basename(output)}",
            self,
        )
        return {"FINISHED"}


def _rebuild_preview(
    scene: bpy.types.Scene, vertices: list[float], triangles: list[int]
) -> None:
    """(Re)build the preview object from a contract-axes triangulation."""
    blender_vertices = [
        axes.convert_point_inverse((vertices[i], vertices[i + 1], vertices[i + 2]))
        for i in range(0, len(vertices), 3)
    ]
    faces = [tuple(triangles[i : i + 3]) for i in range(0, len(triangles), 3)]

    # from_pydata only fills an EMPTY mesh, so each bake builds a fresh datablock and the old
    # one is dropped once nothing references it.
    mesh = bpy.data.meshes.new(PREVIEW_OBJECT_NAME)
    mesh.from_pydata(blender_vertices, [], faces)
    mesh.validate()

    preview = find_preview_object()
    if preview is None:
        preview = bpy.data.objects.new(PREVIEW_OBJECT_NAME, mesh)
        preview[PREVIEW_MARKER] = True
        preview.display_type = "WIRE"
        preview.show_in_front = True
        preview.hide_select = True
        preview.hide_render = True
        scene.collection.objects.link(preview)
    else:
        stale = preview.data
        preview.data = mesh
        if stale is not None and stale.users == 0:
            bpy.data.meshes.remove(stale)


classes = (PARADISE_OT_bake_navmesh,)
