"""Scene walk -> ``data/scenes/<Scene>.json``.

Port of ``SceneDataExporter.ExportRoot``. Walks the scene once, dispatching each object to the
right exporter, then writes the level document plus its side artifacts (materials, project
settings, navmesh).

Since schema v5 the document is a version and a list of objects, and an object is a list of
components — so this walk emits three kinds of object and nothing else:

* every authored entity that says something (:func:`..export.entity.export_entity`);
* every lamp, carrying a Light — a lamp used to be an entry in a document-level lighting state
  unless it happened to be marked as an entity, in which case it travelled as that entity's
  component instead, with a rule saying it must not do both or the runtime would light it twice.
  A lamp is a thing that is placed, and a thing that is placed is an object;
* one object carrying the scene's Environment.

**Order.** Godot walks the scene tree depth-first, so entity order follows the tree. Blender gives
no stable iteration order for ``scene.objects``, so objects are emitted sorted by name (see
:func:`..authoring.entity.entity_objects`). Without that, two exports of an unchanged scene could
differ, which would make every diff and every live-preview patch noisy.
"""

from __future__ import annotations

import os

import bpy

from .. import log
from ..authoring import entity as authoring
from ..contract import component_ids
from ..contract.schema import EntityComponentsData, LevelData
from ..contract.writer import write_json_document
from ..paths import ExportPaths
from ..pipeline import prune
from ..prefs import export_paths
from . import navmesh as navmesh_export
from . import project_settings
from .entity import export_entity, placement_components
from .light import export_light
from .material import MaterialExporter
from .mesh import MeshExporter
from .world import export_environment

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

    document.entities.append(_environment_object(scene, paths))

    for obj in sorted((o for o in scene.objects if o.type == "LIGHT"), key=lambda o: o.name):
        if authoring.is_entity(obj):
            continue  # travels as that object's own Light component (export/entity.py)
        document.entities.append(_light_object(obj, paths))

    for obj in authoring.entity_objects(scene):
        components = export_entity(obj, paths, materials, meshes)
        if components is not None:
            document.entities.append(components)

    if export_assets:
        written = materials.write_exported_materials(paths)
        if written:
            log.info(f"Exported {written} material document(s).")

    return document


def _environment_object(scene: bpy.types.Scene, paths: ExportPaths) -> EntityComponentsData:
    """The scene's lighting and environment, as an object of its own.

    An object with no Blender object behind it, which is why it has no name and no transform: it
    is not placed, and there is nothing in the .blend to point at. The alternative — requiring an
    author to keep a "scene settings" empty around and never delete it — trades a synthetic row in
    the document for a load-bearing object in the scene, which is the worse of the two.
    """
    components = EntityComponentsData(data_dir=paths.data_dir)
    environment = export_environment(scene)

    settings = getattr(scene, "paradise_project", None)
    if settings is not None:
        if settings.shadow_map_size != "DEFAULT":
            environment.shadow_map_size = int(settings.shadow_map_size)
        environment.shadow_blur = round(settings.shadow_blur, 3)

    components.add_engine(component_ids.ENVIRONMENT, environment)
    return components


def _light_object(obj: bpy.types.Object, paths: ExportPaths) -> EntityComponentsData:
    """A lamp that is not an authored entity, as an object carrying its Light.

    Named and placed like everything else. Its light also carries a world-space position and
    direction of its own — the contract has always had both, and the duplication is the light
    record's business rather than something this walk should resolve.
    """
    components = EntityComponentsData(data_dir=paths.data_dir)
    placement_components(obj, components)
    components.add_engine(component_ids.LIGHT, export_light(obj))
    return components


def export_scene(scene: bpy.types.Scene, operator=None, force: bool = False) -> str | None:
    """Full export: level document, materials, project settings, navmesh.

    Returns the written scene JSON path, or ``None`` if the export could not run.

    ``force`` rebuilds every derived artifact, which is what to reach for after changing the
    exporter or the transcoding pipeline: those changes leave every output stale while the
    .blend's mtime — the only staleness signal a scene export has — says nothing happened.
    """
    paths = export_paths(scene)
    paths.ensure_output_directory()

    # The id drift guard, run once per export rather than per entity. The engine component ids in
    # contract/component_ids.py are transcribed from [Guid] attributes by hand and nothing keeps
    # them in step; this checks them against the schema the game's launcher dumped, which
    # describes the engine that game is actually built against. Reported, not fatal — the one
    # failure that must stop an export (a component being written whose CLR name cannot be
    # resolved) already raises, in engine_type_name.
    for drift in component_ids.check_engine_ids(paths.data_dir):
        log.warn(drift, operator)

    scene_name = resolve_scene_name(scene)
    document = build_level_data(scene, paths, force=force)

    project_settings.export_project_settings(paths)
    navmesh_export.export_navmesh(scene, scene_name, paths, force)

    output_path = paths.level_data_output_path(scene_name)
    write_json_document(output_path, document.to_json())

    # After the document is on disk, never before: the sweep reads the scene documents as its
    # roots, so running it earlier would judge this scene's assets against the PREVIOUS export.
    _prune_data_directory(scene, paths, operator)

    log.info(
        f"Exported scene data: {output_path} ({len(document.entities)} objects)", operator)

    # ONE, not zero: the environment object is always written, so an export that found nothing
    # in the .blend still produces a document with a row in it. Counting against 1 rather than
    # against emptiness is what keeps this warning firing at the case it is for.
    if len(document.entities) <= 1:
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
