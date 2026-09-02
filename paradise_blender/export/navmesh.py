"""Navmesh baking via the .NET bridge, since DotRecast has no Python equivalent.

Geometry is the COLLIDER shapes of non-agent entities that have colliders (the Godot host's
``StaticColliders`` filter), never the render mesh: a shelf model over a solid box collider
bakes walkable polys inside a volume the simulation treats as solid, and a planner corner in
there wedges every agent against the real obstacle (ShiningPie's store shelves). Non-box shapes
fall back to the evaluated render mesh. Bake parameters live in the .blend and default to the
Godot host's (cell 0.1, height 1.8, radius 0.4, climb 0.3, slope 45); radius 0 makes paths run
flush against faces and capsules grind along walls. A failed bake never aborts the export.

The bake costs ~3.5 s on ShiningPie (almost all ``dotnet run`` startup), so it is cached on the
complete bridge payload (:mod:`..pipeline.cache`); the bridge's own build is covered by
:func:`..pipeline.bridge.bridge_identity`, and the panel button always re-bakes.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

import bpy
from mathutils import Vector

from .. import log
from ..authoring import authored_components
from ..authoring import entity as authoring
from ..contract import authoring_router, axes
from ..paths import ExportPaths
from ..pipeline.cache import artifact_cache, digest

__all__ = [
    "BAKE_SETTINGS",
    "bake_navmesh",
    "bake_settings",
    "collect_walkable_geometry",
    "export_navmesh",
]

CACHE_KIND = "navmesh"

#: The Godot host's ``NavMeshBake.cs`` defaults; also the fallback for a scene with no settings.
BAKE_SETTINGS = {
    "cellSize": 0.1,
    "cellHeight": 0.1,
    "agentHeight": 1.8,
    "agentRadius": 0.4,
    "agentMaxClimb": 0.3,
    "agentMaxSlope": 45.0,
}


def bake_settings(scene: bpy.types.Scene) -> dict[str, float]:
    """The scene's bake parameters in the bridge's ``BakeSettings`` shape."""
    props = getattr(scene, "paradise_project", None)
    if props is None:
        return dict(BAKE_SETTINGS)

    return {
        "cellSize": props.navmesh_cell_size,
        "cellHeight": props.navmesh_cell_height,
        "agentHeight": props.navmesh_agent_height,
        "agentRadius": props.navmesh_agent_radius,
        "agentMaxClimb": props.navmesh_agent_max_climb,
        "agentMaxSlope": props.navmesh_agent_max_slope,
    }


def export_navmesh(
    scene: bpy.types.Scene, scene_name: str, paths: ExportPaths, force: bool = False
) -> None:
    """Bake ``<scene>.navmesh.bin`` beside the document. Nothing in the document names it since
    v5, which is why prune cannot see it (#28)."""
    bake_navmesh(scene, scene_name, paths, force=force)


def bake_navmesh(
    scene: bpy.types.Scene,
    scene_name: str,
    paths: ExportPaths,
    debug_json_path: str | None = None,
    force: bool = False,
) -> str | None:
    """Run the Recast bake; ``None`` when nothing was baked (a degraded state, not an error).
    ``force`` and ``debug_json_path`` always bake for real: the preview triangulation comes from
    the bridge run and is not part of the cached artifact."""
    try:
        vertices, triangles = collect_walkable_geometry(scene, paths.data_dir)
    except Exception as error:  # a bad mesh must not abort the scene export
        log.warn(f"NavMesh geometry collection failed: {error}")
        return None

    if not triangles:
        # Silent: warning on every export of a scene under construction trains authors to
        # ignore the log.
        return None

    from ..pipeline.bridge import bridge_identity, resolve_bridge_command

    command = resolve_bridge_command()
    if command is None:
        log.warn(
            "NavMesh baking needs the .NET bridge (tools/ParadiseBlenderBridge). Set its path "
            "in the addon preferences, or install the .NET SDK. The scene exports without a "
            "navmesh; agents will have nothing to path on."
        )
        return None

    output_path = paths.nav_mesh_output_path(scene_name)
    input_path = os.path.join(tempfile.gettempdir(), f"paradise_navmesh_{scene_name}.json")
    argv = [*command, "navmesh", "--input", input_path, "--output", output_path]
    if debug_json_path is not None:
        argv += ["--debug-json", debug_json_path]

    # sort_keys: this string IS the cache key. Vertex order is left as the depsgraph yields it;
    # a reordering can only cause a miss, never a stale hit.
    payload = json.dumps(
        {"vertices": vertices, "triangles": triangles, "settings": bake_settings(scene)},
        sort_keys=True,
        separators=(",", ":"),
    )
    cache = artifact_cache(paths)
    key = digest(payload, bridge_identity(command))

    if not force and debug_json_path is None and cache.fetch(CACHE_KIND, key, output_path):
        log.info(f"NavMesh unchanged; reused the cached bake: {output_path}")
        return output_path

    try:
        with open(input_path, "w", encoding="utf-8") as handle:
            handle.write(payload)

        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

        if result.returncode != 0:
            log.warn(f"NavMesh bake failed: {result.stderr.strip() or result.stdout.strip()}")
            return None

        cache.store(CACHE_KIND, key, output_path)
        log.info(f"Exported navmesh: {output_path}")
        return output_path
    except subprocess.TimeoutExpired:
        log.warn("NavMesh bake timed out after 5 minutes; the scene exports without a navmesh.")
        return None
    except OSError as error:
        log.warn(f"NavMesh bake could not run: {error}")
        return None
    finally:
        if os.path.exists(input_path):
            os.unlink(input_path)


def collect_walkable_geometry(
    scene: bpy.types.Scene,
    data_dir: str,
) -> tuple[list[float], list[int]]:
    """Flat world-space triangles of static, collidable entities, in contract axes. Winding
    is untouched: the basis change is a proper rotation and cannot flip a face."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    vertices: list[float] = []
    triangles: list[int] = []

    for obj in authoring.entity_objects(scene):
        # An agent stands ON the navmesh; baking its capsule would punch a hole where it spawns.
        if authored_components.has_component(obj, authoring_router.AGENT):
            continue
        # Resolved once: collider_entries reads the schema document each call.
        entries = authored_components.collider_entries(obj, data_dir)
        if not entries:
            continue
        # A dynamic body baked at its spawn leaves a permanent hole where the car started.
        if authored_components.stored_value(obj, authoring_router.RIGIDBODY, "BodyType") == "Dynamic":
            continue

        if not _append_colliders(entries, vertices, triangles) and obj.type == "MESH":
            # No box collider could be emitted (non-box shapes, or dangling references):
            # the render mesh is the best remaining approximation of what the runtime blocks.
            _append_object(obj, depsgraph, vertices, triangles)

    return vertices, triangles


#: Unit-box corner offsets (to be scaled by half-extents) and the twelve triangles over
#: them, wound counter-clockwise seen from outside — Recast reads slope off the triangle
#: normal, so the top face must genuinely face up.
_BOX_CORNERS = (
    (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
    (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
)
_BOX_TRIANGLES = (
    (0, 2, 1), (0, 3, 2),  # bottom (-z)
    (4, 5, 6), (4, 6, 7),  # top (+z)
    (0, 1, 5), (0, 5, 4),  # front (-y)
    (2, 3, 7), (2, 7, 6),  # back (+y)
    (0, 4, 7), (0, 7, 3),  # left (-x)
    (1, 2, 6), (1, 6, 5),  # right (+x)
)


def _append_colliders(
    entries, vertices: list[float], triangles: list[int]
) -> bool:
    """Append world-space triangles for the entity's box colliders; True if any was emitted.
    Triggers are skipped, mirroring the runtime's obstacle collection."""
    from ..authoring.collider import collider_dimensions, is_collider
    from ..contract.schema import PhysicsShapeType

    emitted = False
    for reference in entries:
        target = reference.target
        if target is None or not is_collider(target):
            continue
        collider = target.paradise_collider
        if collider.is_trigger or collider.shape != PhysicsShapeType.BOX:
            continue

        size, _, _ = collider_dimensions(target)
        half = (size[0] / 2.0, size[1] / 2.0, size[2] / 2.0)
        matrix = target.matrix_world
        base = len(vertices) // 3
        for cx, cy, cz in _BOX_CORNERS:
            world = matrix @ Vector((cx * half[0], cy * half[1], cz * half[2]))
            x, y, z = axes.convert_point((world.x, world.y, world.z))
            vertices.extend((x, y, z))
        for triangle in _BOX_TRIANGLES:
            triangles.extend(base + index for index in triangle)
        emitted = True

    return emitted


def _append_object(
    obj: bpy.types.Object, depsgraph, vertices: list[float], triangles: list[int]
) -> None:
    """Append one object's evaluated, triangulated, world-space geometry."""
    evaluated = obj.evaluated_get(depsgraph)
    try:
        mesh = evaluated.to_mesh()
    except RuntimeError:
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
