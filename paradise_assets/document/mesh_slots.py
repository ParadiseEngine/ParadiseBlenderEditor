"""Keeping ``MaterialsComponentData.Slots`` in step with a mesh's cooked draw list (#67).

``AssetExtractor.Seed`` writes a starter prefab once when a model is first extracted and never
updates it afterwards; a hand-authored or selection-composed document that embeds ``StaticMesh``
and ``MaterialsComponentData`` inline owns its copy the same way. When a model's primitive list
later grows or shrinks -- e.g. assigning an existing material to a few faces splits a draw in
the exported GLB -- a stale ``Slots`` array shifts every draw after the edit by one.

Order is normative and mirrors C# ``GltfSceneReader.BakeInstances`` + ``GltfCook.Cook``: draws
walk the default scene's root nodes depth-first in glTF array order, then each mesh's primitives
in order. Each primitive's glTF material index maps through ``<source>.meta``'s ``[extract]``
parts (scoped to ``model.asset`` for a multi-asset ``.blend``); ``-1`` (unassigned) maps to the
empty ``{}`` inline table.

Imports no ``bpy``.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass

from . import canonical_toml, gltf, mesh_document, model_source, project, schema, sidecar
from . import guid as document_guid
from .prefab import PrefabComponent, PrefabObject

__all__ = [
    "MATERIALS_ID",
    "MATERIALS_TYPE",
    "SLOTS_FIELD",
    "ModelSlots",
    "extracted_materials",
    "glb_draw_materials",
    "materials_component",
    "model_slots_for",
    "reconcile_entry",
    "reconcile_slots",
    "stale_warning",
]

#: Mirrors ``ModelExtraction.MaterialsComponentId`` / ``MaterialsComponentType`` in C#.
MATERIALS_ID = "bdc4fc87-d7b4-41f1-bc90-fc827005adfc"
MATERIALS_TYPE = "Paradise.Export.Data.MaterialsComponentData"
SLOTS_FIELD = "Slots"


@dataclass(frozen=True)
class ModelSlots:
    """One model's current draw-to-material table, read from its GLB JSON and sidecar."""

    model: model_source.Model
    draw_materials: tuple[int, ...]
    by_index: dict[int, canonical_toml.InlineTable]

    @property
    def draw_count(self) -> int:
        return len(self.draw_materials)

    @property
    def material_guids(self) -> frozenset[str]:
        return frozenset(ref["guid"].lower() for ref in self.by_index.values() if "guid" in ref)

    @property
    def missing_indices(self) -> tuple[int, ...]:
        """Material indices named by draws that ``[extract].parts`` has no document for yet."""
        return tuple(sorted({idx for idx in self.draw_materials if idx >= 0 and idx not in self.by_index}))

    def fresh_slots(self) -> list[canonical_toml.InlineTable] | None:
        """The model's default ``Slots`` list, or ``None`` while :attr:`missing_indices` is non-empty."""
        if self.missing_indices:
            return None
        return [
            canonical_toml.InlineTable() if idx < 0 else canonical_toml.InlineTable(self.by_index[idx])
            for idx in self.draw_materials
        ]


def glb_draw_materials(document: dict) -> tuple[int, ...] | None:
    """Per cooked draw, the glTF material index (or ``-1``), mirroring ``GltfSceneReader`` + ``GltfCook``.

    Returns ``None`` when ``document`` is not a valid glTF root or has an out-of-range / cyclic
    scene graph, so an unreadable file is ignored rather than treated as zero draws.
    """
    if not isinstance(document, dict) or not isinstance(document.get("asset"), dict):
        return None
    scenes = document.get("scenes") or []
    if not isinstance(scenes, list):
        return None
    if not scenes:
        return ()

    scene_index = document.get("scene", 0)
    if not _valid_index(scene_index, len(scenes)):
        return None
    scene_obj = scenes[scene_index]
    if not isinstance(scene_obj, dict):
        return None

    nodes = document.get("nodes") or []
    meshes = document.get("meshes") or []
    if not isinstance(nodes, list) or not isinstance(meshes, list):
        return None

    roots = scene_obj.get("nodes") or []
    if not isinstance(roots, list):
        return None

    pending = list(reversed(roots))
    visit_budget = len(nodes)
    draws: list[int] = []

    while pending:
        node_index = pending.pop()
        if not _valid_index(node_index, len(nodes)):
            return None
        visit_budget -= 1
        if visit_budget < 0:
            return None
        node = nodes[node_index]
        if not isinstance(node, dict):
            return None

        if "mesh" in node and node["mesh"] is not None:
            mesh_index = node["mesh"]
            if not _valid_index(mesh_index, len(meshes)):
                return None
            mesh = meshes[mesh_index]
            primitives = mesh.get("primitives") if isinstance(mesh, dict) else None
            if not isinstance(primitives, list):
                return None
            for primitive in primitives:
                if not isinstance(primitive, dict):
                    return None
                material = primitive.get("material", -1)
                if material is None:
                    material = -1
                elif isinstance(material, bool) or not isinstance(material, int):
                    return None
                draws.append(material)

        children = node.get("children") or []
        if not isinstance(children, list):
            return None
        for child in reversed(children):
            pending.append(child)

    return tuple(draws)


def extracted_materials(
    meta: sidecar.Sidecar | None, asset_guid: str | None = None
) -> dict[int, canonical_toml.InlineTable]:
    """``{glTF material index: InlineTable({guid, path})}`` from ``<source>.meta``'s ``[extract]``."""
    if meta is None:
        return {}
    extract = meta.setting("extract")
    parts = extract.get("parts") if isinstance(extract, dict) else None
    if not isinstance(parts, list):
        return {}

    target_asset = document_guid.canonical(asset_guid) if document_guid.is_text(asset_guid) else None
    by_index: dict[int, canonical_toml.InlineTable] = {}
    for part in parts:
        if not isinstance(part, dict) or part.get("kind") != "materials":
            continue
        raw_asset = part.get("asset")
        if raw_asset is None:
            part_asset = None
        elif document_guid.is_text(raw_asset):
            part_asset = document_guid.canonical(raw_asset)
        else:
            continue
        if part_asset != target_asset:
            continue
        index = part.get("index")
        part_guid = part.get("guid")
        part_path = part.get("path")
        if (
            isinstance(index, int)
            and not isinstance(index, bool)
            and index >= 0
            and document_guid.is_text(part_guid)
            and isinstance(part_path, str)
            and part_path
        ):
            by_index[index] = canonical_toml.InlineTable(
                {"guid": document_guid.canonical(part_guid), "path": part_path}
            )
    return by_index


def model_slots_for(
    layout: project.ProjectLayout | None, mesh_path: str | None
) -> ModelSlots | None:
    """The current :class:`ModelSlots` for ``mesh_path``, or ``None`` when no current GLB exists."""
    if layout is None or not isinstance(mesh_path, str) or not mesh_document.is_document(mesh_path):
        return None
    model = mesh_document.source_for(layout, mesh_path)
    if model is None or not os.path.isfile(model.path):
        return None
    glb = model_source.current_glb(model.path, model.asset)
    if glb is None:
        return None
    draws = glb_draw_materials(gltf.read_json(glb))
    if draws is None:
        return None
    meta = sidecar.read(sidecar.path_for(model.path))
    return ModelSlots(model=model, draw_materials=draws, by_index=extracted_materials(meta, model.asset))


def reconcile_slots(
    stored: list,
    model_slots: ModelSlots,
    *,
    layout: project.ProjectLayout | None = None,
    label: str = "",
) -> tuple[list, bool]:
    """Align ``stored`` ``Slots`` to ``model_slots`` when the model's draw count changed.

    - Unchanged draw count keeps ``stored`` verbatim, preserving authored overrides and the
      save's byte-exact fixed point.
    - When the draw count changed and every draw's material is extracted, ``SequenceMatcher``
      aligns slots by canonical material GUID: inserted draws take their extracted material,
      deleted draws drop, and unmatched gaps preserve authored overrides on their corresponding
      draws instead of shifting every later slot by position.
    - When the draw count changed and the GLB names a material index not yet in ``[extract]``,
      raises ``ValueError`` naming the model and the ``paradise assets extract`` command.
    """
    missing = model_slots.missing_indices
    if missing:
        if len(stored) != model_slots.draw_count:
            source_label = (
                layout.relative(model_slots.model.path)
                if layout is not None
                else model_slots.model.label
            )
            prefix = f"{label}: " if label else ""
            indices = ", ".join(str(i) for i in missing)
            raise ValueError(
                f"{prefix}MaterialsComponentData.Slots has {len(stored)} slot(s), but "
                f"'{model_slots.model.label}' now has {model_slots.draw_count} draw(s) and "
                f"material index(es) {indices} have not been extracted yet. "
                f"Run `paradise assets extract {source_label}` first."
            )
        return stored, False

    fresh = model_slots.fresh_slots()
    assert fresh is not None
    if len(stored) == len(fresh):
        return stored, False

    stored_keys = [_slot_key(item) for item in stored]
    fresh_keys = [_slot_key(item) for item in fresh]
    model_guids = model_slots.material_guids

    rebuilt: list[canonical_toml.InlineTable] = []
    matcher = difflib.SequenceMatcher(None, stored_keys, fresh_keys, autojunk=False)
    for tag, s0, s1, f0, f1 in matcher.get_opcodes():
        if tag in ("equal", "insert"):
            rebuilt.extend(canonical_toml.InlineTable(fresh[k]) for k in range(f0, f1))
        elif tag == "delete":
            continue
        elif tag == "replace":
            s_slice = stored[s0:s1]
            f_slice = fresh[f0:f1]
            if len(s_slice) == len(f_slice):
                rebuilt.extend(_as_inline(item) for item in s_slice)
            else:
                for k, fallback in enumerate(f_slice):
                    if k < len(s_slice) and _is_foreign_override(s_slice[k], fallback, model_guids):
                        rebuilt.append(_as_inline(s_slice[k]))
                    else:
                        rebuilt.append(canonical_toml.InlineTable(fallback))

    return rebuilt, rebuilt != stored


def materials_component(entry: PrefabObject) -> PrefabComponent | None:
    """The non-removed ``MaterialsComponentData`` on ``entry``, if any."""
    for component in entry.components:
        if component.removed:
            continue
        if component.id.lower() == MATERIALS_ID or component.type == MATERIALS_TYPE:
            return component
    return None


def reconcile_entry(
    entry: PrefabObject,
    layout: project.ProjectLayout | None,
    mesh_fields: schema.MeshFields,
    *,
    fallback_components: list | None = None,
    label: str = "",
) -> int:
    """Reconcile ``entry``'s ``MaterialsComponentData.Slots`` against its mesh's current GLB.

    ``fallback_components`` supplies the resolved component list for an instance or override
    carrier whose ``entry`` overrides ``MaterialsComponentData`` while inheriting the mesh
    component from its prefab. Returns ``1`` when ``Slots`` changed, else ``0``.
    """
    component = materials_component(entry)
    if component is None:
        return 0
    stored = component.data.get(SLOTS_FIELD)
    if not isinstance(stored, list):
        return 0

    mesh_path = _mesh_path_in_entry(entry, mesh_fields)
    if mesh_path is None and fallback_components is not None:
        mesh_path = _mesh_path_in_payload(fallback_components, mesh_fields)
    model_slots = model_slots_for(layout, mesh_path)
    if model_slots is None:
        return 0

    updated, changed = reconcile_slots(
        stored, model_slots, layout=layout, label=label or entry.name or entry.guid or ""
    )
    if not changed:
        return 0
    component.data[SLOTS_FIELD] = updated
    return 1


def stale_warning(
    entry: PrefabObject,
    layout: project.ProjectLayout | None,
    mesh_fields: schema.MeshFields,
    *,
    fallback_components: list | None = None,
) -> str | None:
    """A load-time warning when ``entry``'s ``Slots`` count disagrees with its model's draws."""
    component = materials_component(entry)
    if component is None:
        return None
    stored = component.data.get(SLOTS_FIELD)
    if not isinstance(stored, list):
        return None

    mesh_path = _mesh_path_in_entry(entry, mesh_fields)
    if mesh_path is None and fallback_components is not None:
        mesh_path = _mesh_path_in_payload(fallback_components, mesh_fields)
    model_slots = model_slots_for(layout, mesh_path)
    if model_slots is None or len(stored) == model_slots.draw_count:
        return None

    who = entry.name or entry.guid or "object"
    if model_slots.missing_indices:
        rel = layout.relative(model_slots.model.path) if layout is not None else model_slots.model.label
        return (
            f"{who}: MaterialsComponentData.Slots has {len(stored)} slot(s), but "
            f"'{model_slots.model.label}' has {model_slots.draw_count} draw(s); "
            f"run `paradise assets extract {rel}` and Save to update it"
        )
    return (
        f"{who}: MaterialsComponentData.Slots has {len(stored)} slot(s), but "
        f"'{model_slots.model.label}' has {model_slots.draw_count} draw(s); Save to update it"
    )


def _valid_index(value: object, count: int) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and 0 <= value < count


def _slot_key(slot: object) -> str:
    if isinstance(slot, dict):
        raw = slot.get("guid")
        if document_guid.is_text(raw):
            return document_guid.canonical(raw)
        if not slot:
            return ""
    return repr(slot)


def _as_inline(slot: object) -> canonical_toml.InlineTable:
    if isinstance(slot, canonical_toml.InlineTable):
        return canonical_toml.InlineTable(slot)
    if isinstance(slot, dict) and canonical_toml.is_written_inline(slot):
        return canonical_toml.InlineTable(slot)
    return canonical_toml.InlineTable()


def _is_foreign_override(slot: object, fallback: dict, model_guids: frozenset[str]) -> bool:
    if not isinstance(slot, dict):
        return False
    if not slot:
        return bool(fallback)
    raw = slot.get("guid")
    return document_guid.is_text(raw) and document_guid.canonical(raw) not in model_guids


def _mesh_path_in_entry(entry: PrefabObject, mesh_fields: schema.MeshFields) -> str | None:
    found = schema.mesh_field(entry.components, mesh_fields)
    if found is None:
        return None
    component, field_name = found
    value = component.data.get(field_name)
    return value.get("path") if isinstance(value, dict) else (value if isinstance(value, str) else None)


def _mesh_path_in_payload(components: list, mesh_fields: schema.MeshFields) -> str | None:
    for component in components:
        if not isinstance(component, dict) or component.get("removed"):
            continue
        data = component.get("data")
        if not isinstance(data, dict):
            continue
        ctype = component.get("type")
        for field_name, value in data.items():
            path = value.get("path") if isinstance(value, dict) else value
            if isinstance(path, str) and mesh_fields.is_mesh_field(ctype, field_name, path):
                return path
    return None
