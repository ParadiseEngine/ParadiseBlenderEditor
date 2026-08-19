"""Scene walk -> ``data/scenes/<Scene>.json``.

Port of ``SceneDataExporter.ExportRoot``. Walks the scene once, dispatching each object to the
right exporter, then writes the level document plus its side artifacts (materials, prefab
templates, project settings, navmesh).

Two structural differences from the Godot host:

* **Order.** Godot walks the scene tree depth-first, so entity order follows the tree. Blender
  gives no stable iteration order for ``scene.objects``, so entities are emitted sorted by
  name (see :func:`..authoring.entity.entity_objects`). Without that, two exports of an
  unchanged scene could differ, which would make every diff and every live-preview patch
  noisy.

* **Identity sweep.** GUID minting runs before the walk rather than on save alone, so an
  unsaved .blend still exports unique identities.
"""

from __future__ import annotations

import os

import bpy

from .. import log
from ..authoring import entity as authoring
from ..authoring.guid import ensure_unique_guids
from ..contract.schema import LevelData
from ..contract.writer import write_json_document
from ..paths import ExportPaths
from ..pipeline import prune
from ..prefs import export_paths
from . import navmesh as navmesh_export
from . import project_settings
from .camera import export_camera, find_camera
from .entity import export_entity
from .light import export_light
from .material import MaterialExporter
from .mesh import MeshExporter
from .prefab import PrefabExporter
from .world import export_environment, resolve_background_color

__all__ = ["build_level_data", "export_scene", "resolve_scene_name"]


def resolve_scene_name(scene: bpy.types.Scene) -> str:
    """Output name for ``data/scenes/<name>.json``.

    Precedence: an explicit override, then the .blend filename (matching the Godot host's rule
    of using the scene file's basename), then the Blender scene's own name for an unsaved file.
    """
    override = scene.paradise_project.scene_name_override.strip()
    if override:
        return override
    if bpy.data.filepath:
        return os.path.splitext(os.path.basename(bpy.data.filepath))[0]
    return scene.name


def build_level_data(
    scene: bpy.types.Scene, paths: ExportPaths, export_assets: bool = True, force: bool = False
) -> LevelData:
    """Build the level document, optionally exporting mesh/material side artifacts.

    ``export_assets=False`` is the live-preview path: re-exporting every GLB on each transform
    tweak would stall Blender, and the runtime already has the meshes loaded.

    ``force`` rebuilds every asset from scratch, ignoring the staleness check and the artifact
    cache -- see :class:`.mesh.MeshExporter`.
    """
    document = LevelData()

    materials = MaterialExporter()
    meshes = MeshExporter(force)
    prefabs = PrefabExporter(materials, meshes)

    camera = find_camera(scene)
    if camera is not None:
        document.camera = export_camera(camera, resolve_background_color(scene.world))

    state = document.ensure_lighting_state()
    settings = getattr(scene, "paradise_project", None)
    if settings is not None:
        if settings.shadow_map_size != "DEFAULT":
            document.lighting.shadow_map_size = int(settings.shadow_map_size)
        document.lighting.shadow_blur = round(settings.shadow_blur, 3)
    state.environment = export_environment(scene)
    for obj in sorted(
        (o for o in scene.objects if o.type == "LIGHT"), key=lambda o: o.name
    ):
        if authoring.is_entity(obj):
            continue  # travels as that entity's Components.Light instead (export/entity.py)
        state.lights.append(export_light(obj))

    for obj in authoring.entity_objects(scene):
        document.entities.append(export_entity(obj, paths, materials, meshes, prefabs))

    if export_assets:
        written = materials.write_exported_materials(paths)
        if written:
            log.info(f"Exported {written} material document(s).")

    return document


def export_scene(scene: bpy.types.Scene, operator=None, force: bool = False) -> str | None:
    """Full export: level document, materials, prefabs, project settings, navmesh.

    Returns the written scene JSON path, or ``None`` if the export could not run.

    ``force`` rebuilds every derived artifact, which is what to reach for after changing the
    exporter or the transcoding pipeline: those changes leave every output stale while the
    .blend's mtime — the only staleness signal a scene export has — says nothing happened.
    """
    paths = export_paths(scene)
    paths.ensure_output_directory()

    repaired = ensure_unique_guids(scene)
    if repaired:
        log.info(f"Minted or repaired {repaired} entity GUID(s) before export.", operator)

    scene_name = resolve_scene_name(scene)
    document = build_level_data(scene, paths, force=force)

    project_settings.export_project_settings(paths)
    navmesh_export.export_navmesh(scene, scene_name, paths, document, force)

    output_path = paths.level_data_output_path(scene_name)
    write_json_document(output_path, document.to_json())

    # After the document is on disk, never before: the sweep reads the scene documents as its
    # roots, so running it earlier would judge this scene's assets against the PREVIOUS export.
    _prune_data_directory(scene, paths, operator)

    log.info(
        f"Exported scene data: {output_path} "
        f"({len(document.entities)} entities, "
        f"{len(document.lighting.states[0].lights) if document.lighting else 0} lights)",
        operator,
    )

    if not document.entities:
        log.warn(
            "No Paradise entities were found, so the exported scene will render empty. Select "
            "objects and use Make Paradise Entity in the Paradise panel.",
            operator,
        )

    return output_path


#: How many removed files to name in the log before summarizing. A first cleanup of a long-lived
#: project can remove hundreds; a wall of paths would bury everything else the export said.
_PRUNE_LOG_LIMIT = 20


def _prune_data_directory(scene: bpy.types.Scene, paths: ExportPaths, operator) -> None:
    """Delete artifacts this scene no longer references, if the project asks for it.

    Reported by name rather than by count alone: this is the only step of an export that removes
    something an author might still care about, and "3 file(s) removed" is not something you can
    check. The files are recoverable from git, and a deleted texture comes back from the artifact
    cache on the next export without re-encoding.

    Absent settings mean "do not prune", not "prune". The switch defaults to off precisely so
    that deletion is something a project opts into, and a scene with no property group attached
    (a non-standard scene, a tooling or test context) has opted into nothing. For a destructive
    step the unknown case has to fall on the side that does nothing.
    """
    settings = getattr(scene, "paradise_project", None)
    if settings is None or not settings.prune_data:
        return

    removed = prune.prune_orphans(paths)
    if not removed:
        return

    for field in removed[:_PRUNE_LOG_LIMIT]:
        log.info(f"Removed unreferenced {field}")
    if len(removed) > _PRUNE_LOG_LIMIT:
        log.info(f"...and {len(removed) - _PRUNE_LOG_LIMIT} more")
    log.info(f"Removed {len(removed)} unreferenced file(s) from the data directory.", operator)
