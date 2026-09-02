"""Scene walk -> ``data/scenes/<Scene>.json`` (port of ``SceneDataExporter.ExportRoot``).

Emits every entity that authors something, every lamp as an object carrying a Light (an id the
game must declare, or :func:`~..contract.component_ids.engine_type_name` raises), and one
Environment object. Placement is a second pass over the surviving set (:mod:`.placement`). Objects are
sorted by name because Blender guarantees no iteration order, and an unstable order makes every
diff and live-preview patch noisy.
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
from .entity import export_entity
from .light import export_light
from .material import MaterialExporter
from .mesh import MeshExporter
from .placement import Placement
from .world import export_environment

__all__ = ["build_level_data", "export_scene", "resolve_scene_name"]


def resolve_scene_name(scene: bpy.types.Scene) -> str:
    """Output name: the override, else the .blend basename (the Godot rule), else the scene name."""
    override = scene.paradise_project.scene_name_override.strip()
    if override:
        return override
    if bpy.data.filepath:
        return os.path.splitext(os.path.basename(bpy.data.filepath))[0]
    return scene.name


def build_level_data(
    scene: bpy.types.Scene, paths: ExportPaths, export_assets: bool = True, force: bool = False
) -> LevelData:
    """Build the level document. ``export_assets=False`` is the live-preview path, where
    re-exporting every GLB per transform tweak would stall Blender."""
    document = LevelData()

    materials = MaterialExporter(paths)
    meshes = MeshExporter(force)

    document.entities.append(_environment_object(scene, paths))

    # Two passes: a transform is local to the nearest EXPORTED ancestor, known only after
    # every object has been offered.
    emitted: list[tuple[bpy.types.Object, EntityComponentsData]] = []

    for obj in sorted((o for o in scene.objects if o.type == "LIGHT"), key=lambda o: o.name):
        if authoring.is_entity(obj):
            continue  # travels as that object's own Light component (export/entity.py)
        emitted.append((obj, _light_object(obj, paths)))

    for obj in authoring.entity_objects(scene):
        components = export_entity(obj, paths, materials, meshes)
        if components is not None:
            emitted.append((obj, components))

    placement = Placement({obj.name for obj, _ in emitted})
    for obj, components in emitted:
        placement.components(obj, components)
        document.entities.append(components)

    if export_assets:
        written = materials.write_exported_materials(paths)
        if written:
            log.info(f"Exported {written} material document(s).")

    return document


def _environment_object(scene: bpy.types.Scene, paths: ExportPaths) -> EntityComponentsData:
    """The Environment as a synthetic object with no name or transform, rather than requiring
    the author to keep a load-bearing "scene settings" empty around."""
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
    """A non-entity lamp as an object carrying its Light."""
    components = EntityComponentsData(data_dir=paths.data_dir)
    components.add_engine(component_ids.LIGHT, export_light(obj))
    return components


def export_scene(scene: bpy.types.Scene, operator=None, force: bool = False) -> str | None:
    """Full export; returns the scene JSON path or ``None``. ``force`` rebuilds every artifact,
    the only remedy for a change to the exporter itself, which no mtime can see."""
    paths = export_paths(scene)
    paths.ensure_output_directory()

    # Id drift guard, reported not fatal; the one fatal case already raises in engine_type_name.
    for drift in component_ids.check_engine_ids(paths.data_dir):
        log.warn(drift, operator)

    scene_name = resolve_scene_name(scene)
    document = build_level_data(scene, paths, force=force)

    project_settings.export_project_settings(paths)
    navmesh_export.export_navmesh(scene, scene_name, paths, force)

    output_path = paths.level_data_output_path(scene_name)
    write_json_document(output_path, document.to_json())

    # After the document is on disk: the sweep's roots are the documents, so earlier would
    # judge against the PREVIOUS export.
    _prune_data_directory(scene, paths, operator)

    log.info(
        f"Exported scene data: {output_path} ({len(document.entities)} objects)", operator)

    # One, not zero: the environment object is always written.
    if len(document.entities) <= 1:
        log.warn(
            "No Paradise entities were found, so the exported scene will render empty. Select "
            "objects and use Make Paradise Entity in the Paradise panel.",
            operator,
        )

    return output_path


#: Removed files named before summarizing; a first cleanup can remove hundreds.
_PRUNE_LOG_LIMIT = 20


def _prune_data_directory(scene: bpy.types.Scene, paths: ExportPaths, operator) -> None:
    """Prune if the project opted in. Absent settings mean "do not prune": for a destructive
    step the unknown case must do nothing. Reported by name, since a count cannot be checked."""
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
