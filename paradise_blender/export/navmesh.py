"""Navmesh baking via the .NET bridge.

Navmesh is the one part of the contract that cannot be produced in Python. The runtime reads a
DotRecast ``MeshSet`` binary, and DotRecast is a C# library with no Python equivalent -- so
this module collects walkable geometry from the Blender scene and hands it to
``tools/ParadiseBlenderBridge``, which runs the Recast bake and writes the binary using the
same ``Paradise.Export.NavMesh.NavMeshBinaryWriter`` the Godot host uses. Same library, same
quantization, same output format.

The Godot host bakes from ``NavigationMesh.ParsedGeometryType.StaticColliders``, which
naturally excludes moving agents. Blender has no collision-shape parsing, so the equivalent
filter here is: **entities that are not agents and do have physics colliders**. An entity with
no colliders is scenery you can walk through, and an agent is the thing doing the walking --
neither belongs in the walkable surface.

Bake parameters mirror the Godot host's exactly (cell 0.1, agent height 1.8, radius 0.4, max
climb 0.3), so a scene authored in either tool produces the same navmesh. The radius in
particular is not a default to tidy up: with radius 0 the planned paths run flush against
obstacle faces and agent capsules grind along walls.

A failed or skipped bake leaves ``NavMeshFile`` null rather than aborting the scene export --
a scene with no walkable geometry is perfectly valid.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

import bpy

from .. import log
from ..authoring import entity as authoring
from ..contract import axes
from ..contract.schema import LevelData
from ..paths import ExportPaths

__all__ = ["BAKE_SETTINGS", "collect_walkable_geometry", "export_navmesh"]

#: Must match ``NavMeshBake.cs`` and ``NavMeshBinaryWriter``'s quantization.
BAKE_SETTINGS = {
    "cellSize": 0.1,
    "cellHeight": 0.1,
    "agentHeight": 1.8,
    "agentRadius": 0.4,
    "agentMaxClimb": 0.3,
}


def export_navmesh(
    scene: bpy.types.Scene, scene_name: str, paths: ExportPaths, document: LevelData
) -> None:
    """Bake and record the navmesh, or leave ``NavMeshFile`` null."""
    try:
        vertices, triangles = collect_walkable_geometry(scene)
    except Exception as error:  # a bad mesh must not abort the scene export
        log.warn(f"NavMesh geometry collection failed: {error}")
        return

    if not triangles:
        # Silent: most scenes under construction have no walkable geometry yet, and warning
        # on every export would train authors to ignore the log.
        return

    from ..pipeline.bridge import resolve_bridge_command

    command = resolve_bridge_command()
    if command is None:
        log.warn(
            "NavMesh baking needs the .NET bridge (tools/ParadiseBlenderBridge). Set its path "
            "in the addon preferences, or install the .NET SDK. The scene exports without a "
            "navmesh; agents will have nothing to path on."
        )
        return

    output_path = paths.nav_mesh_output_path(scene_name)
    input_path = os.path.join(tempfile.gettempdir(), f"paradise_navmesh_{scene_name}.json")

    try:
        with open(input_path, "w", encoding="utf-8") as handle:
            json.dump(
                {"vertices": vertices, "triangles": triangles, "settings": BAKE_SETTINGS}, handle
            )

        result = subprocess.run(
            [*command, "navmesh", "--input", input_path, "--output", output_path],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

        if result.returncode != 0:
            log.warn(f"NavMesh bake failed: {result.stderr.strip() or result.stdout.strip()}")
            return

        document.nav_mesh_file = paths.nav_mesh_file_field(scene_name)
        log.info(f"Exported navmesh: {output_path}")
    except subprocess.TimeoutExpired:
        log.warn("NavMesh bake timed out after 5 minutes; the scene exports without a navmesh.")
    except OSError as error:
        log.warn(f"NavMesh bake could not run: {error}")
    finally:
        if os.path.exists(input_path):
            os.unlink(input_path)


def collect_walkable_geometry(
    scene: bpy.types.Scene,
) -> tuple[list[float], list[int]]:
    """Triangulated world-space geometry of static, collidable entities, in contract axes.

    Returns a flat ``[x, y, z, ...]`` vertex list and a flat triangle index list -- the shape
    ``NavMeshBinaryWriter`` consumes.

    Winding passes through unchanged: the basis change is a proper rotation, so it cannot flip
    face orientation (see :func:`..contract.axes.convert_triangle_indices`).
    """
    depsgraph = bpy.context.evaluated_depsgraph_get()
    vertices: list[float] = []
    triangles: list[int] = []

    for obj in authoring.entity_objects(scene):
        props = obj.paradise
        if props.is_agent or not len(props.physics_colliders):
            continue
        # Dynamic bodies move; baking one freezes it into the walkable surface at its SPAWN --
        # the car would leave a permanent hole in the navmesh where it started. The Godot host
        # gets this for free by parsing StaticColliders only; this is the same filter.
        if props.is_dynamic_body:
            continue
        if obj.type != "MESH":
            continue

        _append_object(obj, depsgraph, vertices, triangles)

    return vertices, triangles


def _append_object(
    obj: bpy.types.Object, depsgraph, vertices: list[float], triangles: list[int]
) -> None:
    """Append one object's evaluated, triangulated, world-space geometry."""
    evaluated = obj.evaluated_get(depsgraph)
    try:
        mesh = evaluated.to_mesh()
    except RuntimeError:
        # Objects with no evaluable geometry (an empty mesh, a failed modifier stack).
        return

    if mesh is None:
        return

    try:
        # loop_triangles is the triangulated view of an n-gon mesh; Recast needs triangles.
        mesh.calc_loop_triangles()
        base = len(vertices) // 3
        matrix = evaluated.matrix_world

        for vertex in mesh.vertices:
            world = matrix @ vertex.co
            x, y, z = axes.convert_point((world.x, world.y, world.z))
            vertices.extend((x, y, z))

        for triangle in mesh.loop_triangles:
            triangles.extend(base + index for index in triangle.vertices)
    finally:
        evaluated.to_mesh_clear()
