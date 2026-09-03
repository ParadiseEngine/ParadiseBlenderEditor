"""On-demand navmesh bake with a wireframe preview built from the triangulation the bridge
ACTUALLY wrote (``--debug-json``), so erosion and doorway cuts are visible. The preview object
is inert (not an entity, unselectable) and drawn in front, since the surface sits a cell above
the floor and would z-fight.
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
                # report() so the summary reaches the status bar, not only the console.
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

        # Baking is a request to look: do not leave the preview hidden behind a forgotten toggle.
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
