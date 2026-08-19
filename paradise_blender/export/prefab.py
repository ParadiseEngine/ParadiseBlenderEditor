"""Prefab identity and templates.

The Godot host's prefab model is ``PackedScene`` instancing: an instanced node carries a
``SceneFilePath`` and resources have stable ``uid://`` ids. Blender's closest equivalent is
**collection instancing** -- an Empty whose ``instance_collection`` points at a collection,
which may itself be linked from another .blend. That maps cleanly:

===========================  ==================================================
contract field               Blender source
===========================  ==================================================
``PrefabAssetPath``          the source .blend library path, or the collection name
``PrefabGuid``               the collection's ``session_uid``, or its library path
``PrefabAssetType``          ``.blend`` for a linked collection, ``.collection`` for a local one
``NearestInstanceRoot``      the instancing Empty's name
===========================  ==================================================

What does **not** map is per-property overrides. Godot exposes no API for them, and Blender's
collection instances cannot override interior properties at all -- overriding in Blender means
a library override, which produces different datablocks rather than a diff. So
``PrefabOverrideData`` is emitted empty by both hosts, and the contract's override fields stay
reserved.

``PrefabGuid`` deserves a caveat: Blender has no persistent per-datablock id. ``session_uid``
is stable within a session but is reassigned on file load, so for a **local** collection the
guid is derived from its name instead. A linked collection uses its library path, which is
genuinely stable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import bpy

from .. import log
from ..contract.schema import (
    LevelEntityData,
    PrefabTemplateData,
    RenderableComponentData,
)
from ..contract.writer import write_json_document
from ..paths import ExportPaths, prefab_file_field

__all__ = ["PrefabExporter", "PrefabIdentity"]


@dataclass(frozen=True)
class PrefabIdentity:
    prefab_asset_path: str | None = None
    prefab_guid: str | None = None
    prefab_asset_type: str | None = None
    nearest_instance_root: str | None = None


class PrefabExporter:
    """Resolves prefab identity for entities and writes one template JSON per prefab."""

    def __init__(self, materials, meshes) -> None:
        self._materials = materials
        self._meshes = meshes
        self._exported: set[str] = set()

    def resolve_and_export(self, obj: bpy.types.Object, paths: ExportPaths) -> PrefabIdentity:
        """Identity from the object's nearest collection-instance ancestor, exporting that
        prefab's template as a side effect (deduplicated)."""
        instance_root = _nearest_instance_root(obj)
        if instance_root is None:
            return PrefabIdentity()

        collection = instance_root.instance_collection
        if collection is None:
            return PrefabIdentity()

        asset_path = _asset_path(collection)
        self._export_template(collection, asset_path, paths)

        return PrefabIdentity(
            prefab_asset_path=asset_path,
            prefab_guid=_prefab_guid(collection),
            prefab_asset_type=".blend" if collection.library is not None else ".collection",
            nearest_instance_root=instance_root.name,
        )

    def _export_template(
        self, collection: bpy.types.Collection, asset_path: str, paths: ExportPaths
    ) -> None:
        if asset_path in self._exported:
            return
        self._exported.add(asset_path)

        template = PrefabTemplateData(
            display_name=collection.name,
            prefab=None,
            prefab_asset_path=asset_path,
            prefab_guid=_prefab_guid(collection),
            prefab_asset_type=".blend" if collection.library is not None else ".collection",
            entities=_shallow_entities(collection),
        )

        field = prefab_file_field(collection.name)
        write_json_document(paths.output_path_for_field(field), template.to_json())
        log.info(f"Exported prefab template: {field}")


def _shallow_entities(collection: bpy.types.Collection) -> list[LevelEntityData]:
    """Template entities: id/kind/transform/renderable only.

    Deliberately shallow, matching the Godot host: scene *placements* already carry the
    authoritative component data, so a full nested export here would duplicate it and give the
    runtime two sources of truth that can disagree.
    """
    from ..authoring import entity as authoring
    from .transform import decompose_contract

    entities: list[LevelEntityData] = []
    for obj in collection.all_objects:
        if not authoring.is_entity(obj):
            continue

        position, rotation, scale, _matrix = decompose_contract(obj.matrix_local)
        props = obj.paradise
        entities.append(
            LevelEntityData(
                # Template entities carry no placement identity: the GUID is assigned per
                # placement by the scene exporter, and emitting one here would duplicate it.
                id=obj.name,
                stable_id=obj.name,
                # Identity is authored per PLACEMENT (a paradise.identity component on the
                # scene entity); a template carries only the label every entity defaults to.
                kind="Prop",
                spawn_phase="LevelStart",
                prefab=props.model_path.strip() or None,
                local_position=position,
                local_rotation=rotation,
                local_scale=scale,
                components=_template_components(props),
            )
        )
    return entities


def _template_components(props):
    from ..contract.schema import EntityComponentsData

    components = EntityComponentsData()
    if props.model_path.strip():
        components.renderable = RenderableComponentData()
    return components


def _nearest_instance_root(obj: bpy.types.Object) -> bpy.types.Object | None:
    """Nearest ancestor (or the object itself) that instances a collection."""
    current: bpy.types.Object | None = obj
    while current is not None:
        if current.instance_type == "COLLECTION" and current.instance_collection is not None:
            return current
        current = current.parent
    return None


def _asset_path(collection: bpy.types.Collection) -> str:
    """Source identity of a prefab collection.

    A linked collection reports its library .blend; a local one reports its name, since there
    is no file to point at.
    """
    if collection.library is not None:
        return bpy.path.abspath(collection.library.filepath)
    return collection.name


def _prefab_guid(collection: bpy.types.Collection) -> str:
    """Stable-enough prefab identity.

    A linked collection's library path is genuinely stable. A local collection has none --
    Blender's ``session_uid`` is reassigned on load -- so its name is used, which is stable as
    long as nobody renames it. Renaming a prefab collection therefore changes its guid; that
    is a real limitation, not an oversight, and there is no better handle available.
    """
    if collection.library is not None:
        return f"blend://{os.path.basename(collection.library.filepath)}#{collection.name}"
    return f"collection://{collection.name}"
