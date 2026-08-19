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

What gets rasterized for a qualifying entity is its **declared collider shapes, not its
render mesh**. The runtime collides agents against the colliders, so the navmesh must
describe that world: a shelf model full of open geometry over a solid box collider would
otherwise bake walkable polys inside a volume the simulation treats as solid, and a planner
corner in there wedges every agent that follows it against the real obstacle (found the hard
way with ShiningPie's store shelves). Box colliders — the only static shape in practice —
are emitted as twelve exact triangles; any non-box collider falls back to the entity's
evaluated render mesh, the pre-existing behavior.

Bake parameters are authored per scene (``ParadiseScenePreferences``, surfaced in the panel's
NavMesh section) and travel inside the .blend, because they shape exported data. Their
defaults mirror the Godot host's exactly (cell 0.1, agent height 1.8, radius 0.4, max climb
0.3, slope 45), so an untouched scene bakes identically from either tool. The radius in
particular is not a default to tidy up: with radius 0 the planned paths run flush against
obstacle faces and agent capsules grind along walls.

A failed or skipped bake leaves ``NavMeshFile`` null rather than aborting the scene export --
a scene with no walkable geometry is perfectly valid.

**The bake ran on every export**, including the majority that move nothing walkable. It costs
~3.5 s on ShiningPie's district -- nearly all of it ``dotnet run`` starting a process, since the
Recast pass itself sees only 1404 collider triangles -- which is a tenth of the export but the
whole of it once textures are cached. It is now skipped when its input is unchanged: the
geometry-and-settings payload handed to the bridge is the COMPLETE input of the bake, so hashing
that JSON is an exact key rather than a heuristic -- see :mod:`..pipeline.cache`. What the key
cannot see is the bridge's own code changing underneath an unchanged scene;
:func:`..pipeline.bridge.bridge_identity` covers its build output, and the panel's Bake NavMesh
button always re-bakes for real.
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
from ..contract.schema import LevelData
from ..paths import ExportPaths
from ..pipeline.cache import artifact_cache, digest

__all__ = [
    "BAKE_SETTINGS",
    "bake_navmesh",
    "bake_settings",
    "collect_walkable_geometry",
    "export_navmesh",
]

#: Cache namespace for baked navmeshes.
CACHE_KIND = "navmesh"

#: The engine defaults — what ``NavMeshBake.cs`` uses on the Godot host, and the defaults of
#: the per-scene properties below. Kept as the documented reference point (and the fallback
#: when a scene has no Paradise settings registered, e.g. bare unit contexts).
BAKE_SETTINGS = {
    "cellSize": 0.1,
    "cellHeight": 0.1,
    "agentHeight": 1.8,
    "agentRadius": 0.4,
    "agentMaxClimb": 0.3,
    "agentMaxSlope": 45.0,
}


def bake_settings(scene: bpy.types.Scene) -> dict[str, float]:
    """The scene's bake parameters, in the shape the bridge's ``BakeSettings`` deserializes.

    Authored per scene (see ``ParadiseScenePreferences``) because they shape exported data:
    the same .blend must bake the same navmesh on every machine. Defaults mirror the Godot
    host, so an untouched scene still bakes identically from either tool.
    """
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
    scene: bpy.types.Scene,
    scene_name: str,
    paths: ExportPaths,
    document: LevelData,
    force: bool = False,
) -> None:
    """Bake and record the navmesh, or leave ``NavMeshFile`` null."""
    if bake_navmesh(scene, scene_name, paths, force=force) is None:
        return

    document.nav_mesh_file = paths.nav_mesh_file_field(scene_name)


def bake_navmesh(
    scene: bpy.types.Scene,
    scene_name: str,
    paths: ExportPaths,
    debug_json_path: str | None = None,
    force: bool = False,
) -> str | None:
    """Run the Recast bake and write ``scenes/<name>.navmesh.bin``.

    Returns the output path, or ``None`` when there was nothing to bake or the bridge was
    unavailable/failed — every one of which is a degraded state the caller reports, not an
    exception (a scene with no walkable geometry is perfectly valid).

    An unchanged input reuses the cached bake instead of spending ~3.5 s reproducing it. Two
    requests always bake for real: ``force``, and any request for ``debug_json_path``, since the
    triangulation the viewport preview draws is produced by the bridge run itself and is not part
    of the cached artifact.

    ``debug_json_path`` additionally asks the bridge for the baked triangulation (contract
    axes) — what the viewport preview is built from.
    """
    try:
        vertices, triangles = collect_walkable_geometry(scene)
    except Exception as error:  # a bad mesh must not abort the scene export
        log.warn(f"NavMesh geometry collection failed: {error}")
        return None

    if not triangles:
        # Silent: most scenes under construction have no walkable geometry yet, and warning
        # on every export would train authors to ignore the log.
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

    # sort_keys is not cosmetic: this string IS the cache key, so an unstable dict order would
    # mean a fresh bake every time despite an identical scene.
    #
    # Vertex/triangle ORDER is deliberately left as the depsgraph yields it, and does not need to
    # be canonicalized. The failure it could cause only runs one way: reordered geometry hashes
    # differently, which is a cache MISS and a redundant bake, never a stale hit. A stale hit
    # needs the opposite -- changed geometry with an unchanged digest -- which is a SHA-256
    # collision. Sorting here would buy nothing and cost a sort of every vertex on every export.
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
        # An agent stands ON the navmesh; baking its capsule would punch a hole where it spawns.
        if authored_components.has_component(obj, authoring_router.AGENT):
            continue
        if not len(props.physics_colliders):
            continue
        # Dynamic bodies move; baking one freezes it into the walkable surface at its SPAWN --
        # the car would leave a permanent hole in the navmesh where it started. The Godot host
        # gets this for free by parsing StaticColliders only; this is the same filter.
        if authored_components.stored_value(obj, authoring_router.RIGIDBODY, "BodyType") == "Dynamic":
            continue

        if not _append_colliders(obj, vertices, triangles) and obj.type == "MESH":
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
    obj: bpy.types.Object, vertices: list[float], triangles: list[int]
) -> bool:
    """Append world-space triangles for the entity's BOX colliders; True if any was emitted.

    Triggers are not solid and are skipped, mirroring the runtime's obstacle collection. The
    collider object's own world matrix carries every inherited rotation and scale, so the
    emitted box is exactly the volume the contract exports.
    """
    from ..authoring.collider import collider_dimensions, is_collider
    from ..contract.schema import PhysicsShapeType

    emitted = False
    for reference in obj.paradise.physics_colliders:
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
