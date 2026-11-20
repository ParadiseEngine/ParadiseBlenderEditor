"""Detaching objects of a ``.blend`` model source into separate directly-placed models (#66).

Pure document, sidecar and namespace rules -- no ``bpy`` imports -- so every collision check,
remainder identity migration, seed-suppressing placement stub and post-extraction slot fill is
unit-testable without Blender.

The orchestration solves two pipeline invariants without changing the engine's extractor:

1. **Remainder identity preservation**: when a whole-file ``.blend`` becomes multi-asset, the
   remainder collection wraps the rest under the original stem, and its existing ``.mesh`` /
   ``.skeleton`` / ``.anim`` documents plus ``<source>.meta``'s ``[extract].parts`` receive
   ``asset = { guid, name }`` BEFORE ``paradise assets extract`` runs -- otherwise
   ``AssetExtractor.RemoveMissingModels`` treats ``asset = null`` as a removed model and deletes
   the remainder's mesh document.
2. **No seed prefabs minted**: ``AssetExtractor.Seed`` skips generating a starter ``.prefab``
   whenever anything in ``assets/`` already references the model's ``.mesh`` GUID. Writing the
   detached ``.mesh`` documents (with ``.mesh.meta`` sidecars) and their direct ``StaticMesh``
   placements into every placing ``.prefab`` BEFORE ``extract`` makes the extractor skip seed
   generation natively; after ``extract`` writes the material documents, ``finalize_placements``
   fills each detached placement's ``MaterialsComponentData.Slots`` and reconciles the body's.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from . import (
    atomic,
    canonical_toml,
    geometry_prefab,
    mesh_document,
    mesh_slots,
    new_prefab,
    prefab,
    schema,
    sidecar,
    well_known,
)
from . import guid as document_guid
from .prefab import PrefabComponent, PrefabObject
from .project import MANIFEST_NAME, ProjectLayout

__all__ = [
    "DetachError",
    "DetachPlan",
    "DetachedMesh",
    "clean_model_name",
    "finalize_placements",
    "find_body_mesh",
    "find_placing_prefabs",
    "is_document_name",
    "migrate_remainder_attribution",
    "preflight_targets",
    "stage_detached_meshes_and_placements",
]

_RESERVED_CHARS = frozenset('<>:"/\\|?*')
_SKIP_DIRS = frozenset({".git", ".editor", "build", "bin", "obj"})
_MESH_REF_SUFFIXES = (".mesh", ".skinnedmesh", ".skeleton", ".anim")


class DetachError(Exception):
    """Detaching models could not proceed. The message is for the author."""


@dataclass(frozen=True)
class DetachedMesh:
    """One detached asset collection's minted ``.mesh`` document."""

    name: str
    asset_guid: str
    mesh_guid: str
    mesh_abs: str
    mesh_rel: str


@dataclass(frozen=True)
class DetachPlan:
    """Validated preflight plan for detaching models from ``blend_path``."""

    layout: ProjectLayout
    blend_path: str
    source_guid: str
    source_rel: str
    directories: dict[str, str]
    static_mesh_component: schema.MeshComponent
    mesh_fields: schema.MeshFields
    was_whole_file: bool
    body_stem: str
    body_mesh_guid: str
    body_mesh_rel: str
    target_names: tuple[str, ...]
    placing_prefabs: tuple[str, ...]


def is_document_name(name: str) -> bool:
    """Mirrors C# ``BlenderModelConverter.IsDocumentName``."""
    return (
        len(name) > 0
        and name not in (".", "..")
        and name == name.strip()
        and not name.endswith(".")
        and all(ord(ch) >= 32 and ch not in _RESERVED_CHARS for ch in name)
    )


def clean_model_name(raw: str) -> str:
    """Sanitize a Blender object/collection name into a valid model document stem."""
    cleaned = "".join("_" if (ord(ch) < 32 or ch in _RESERVED_CHARS) else ch for ch in raw.strip())
    return cleaned.rstrip(". ")


def preflight_targets(
    layout: ProjectLayout | None,
    blend_path: str,
    body_asset_guid: str | None,
    existing_collection_names: Iterable[str],
    body_stem: str,
    target_names: list[str],
) -> DetachPlan:
    """Validate every filesystem, schema, naming and reference precondition before writing."""
    if not blend_path or not blend_path.lower().endswith(".blend"):
        raise DetachError("Save and open a .blend model source under assets/ before detaching models.")
    blend_abs = os.path.abspath(blend_path)
    if layout is None:
        raise DetachError(f"No asset project (assets/{MANIFEST_NAME}) above {blend_abs}.")

    assets_real = os.path.realpath(layout.assets)
    try:
        inside = os.path.commonpath((os.path.realpath(blend_abs), assets_real)) == assets_real
    except ValueError:
        inside = False
    if not inside or not os.path.isfile(blend_abs):
        raise DetachError("The .blend file must be saved inside the project's assets/ directory.")

    source_meta = sidecar.read(sidecar.path_for(blend_abs))
    if source_meta is None:
        raise DetachError(
            f"'{layout.relative(blend_abs)}' has no .meta sidecar yet. "
            "Run `paradise assets extract` on it before detaching models."
        )

    static_components = [
        item for item in schema.mesh_components(layout.root)
        if "skinned" not in item.type_name.lower()
    ]
    if not static_components:
        raise DetachError(
            "Build the game's launcher first: .editor/authoring-schema.json must declare a "
            "static mesh component before detached models can be placed."
        )
    static_mesh_component = static_components[0]
    mesh_fields = schema.load(layout.root)

    if not target_names:
        raise DetachError("Select at least one mesh object to detach.")

    was_whole_file = body_asset_guid is None
    if was_whole_file and not is_document_name(body_stem):
        raise DetachError(
            f"Remainder model name '{body_stem}' cannot name extracted documents; "
            "rename the .blend without reserved characters."
        )

    existing_folded = {name.casefold(): name for name in existing_collection_names}
    if was_whole_file and body_stem.casefold() in existing_folded:
        # An existing non-asset collection with the stem's name can be reused or wrapped, wait:
        # refuse if an existing collection already claims that name so Blender does not suffix .001.
        raise DetachError(
            f"Collection '{existing_folded[body_stem.casefold()]}' already exists in this .blend; "
            f"rename it so the remainder model can use '{body_stem}'."
        )

    seen_targets: dict[str, str] = {}
    for name in target_names:
        if not is_document_name(name):
            raise DetachError(
                f"Model name '{name}' is not a valid document name (avoid reserved path characters, "
                "leading/trailing spaces, or a trailing dot)."
            )
        folded = name.casefold()
        if folded in seen_targets:
            raise DetachError(
                f"Selected objects map to colliding model names ('{seen_targets[folded]}' and '{name}'). "
                "Rename one before detaching."
            )
        seen_targets[folded] = name
        if was_whole_file and folded == body_stem.casefold():
            raise DetachError(
                f"Detached model name '{name}' collides with the remainder model '{body_stem}'. "
                "Rename the object before detaching."
            )
        if folded in existing_folded:
            raise DetachError(
                f"Collection '{existing_folded[folded]}' already exists in this .blend. "
                f"Rename the object or collection so '{name}' is unique."
            )

    try:
        directories = geometry_prefab.extraction_directories(layout, os.path.dirname(blend_abs))
    except new_prefab.CreateError as error:
        raise DetachError(str(error)) from error

    for name in target_names:
        for destination in directories.values():
            try:
                new_prefab.refuse_target(os.path.join(destination, f"{name}.mesh"), layout)
            except new_prefab.CreateError as error:
                raise DetachError(str(error)) from error
        for directory in set(directories.values()):
            if not os.path.isdir(directory):
                continue
            for entry_name in os.listdir(directory):
                if entry_name.casefold().startswith((name.casefold() + ".", name.casefold() + "_")):
                    collision = layout.relative(os.path.join(directory, entry_name))
                    raise DetachError(
                        f"{collision} already exists in the extraction namespace. "
                        f"Rename '{name}' to avoid colliding with existing extracted assets."
                    )

    canonical_body_asset = (
        document_guid.canonical(body_asset_guid) if document_guid.is_text(body_asset_guid) else None
    )
    found_mesh = find_body_mesh(layout, source_meta.guid, canonical_body_asset)
    if found_mesh is None:
        raise DetachError(
            f"No extracted .mesh document found for '{layout.relative(blend_abs)}'. "
            "Run `paradise assets extract` on the model before detaching parts of it."
        )
    body_mesh_guid, body_mesh_rel = found_mesh

    placing = find_placing_prefabs(layout, body_mesh_guid, body_mesh_rel, mesh_fields)
    if not placing:
        raise DetachError(
            f"Nothing under assets/ places '{body_mesh_rel}' directly. Place the model in a "
            "level or prefab before detaching parts of it into direct placements."
        )

    return DetachPlan(
        layout=layout,
        blend_path=blend_abs,
        source_guid=source_meta.guid,
        source_rel=layout.relative(blend_abs),
        directories=directories,
        static_mesh_component=static_mesh_component,
        mesh_fields=mesh_fields,
        was_whole_file=was_whole_file,
        body_stem=body_stem,
        body_mesh_guid=body_mesh_guid,
        body_mesh_rel=body_mesh_rel,
        target_names=tuple(target_names),
        placing_prefabs=tuple(placing),
    )


def find_body_mesh(
    layout: ProjectLayout, source_guid: str, body_asset_guid: str | None
) -> tuple[str, str] | None:
    """Return ``(mesh_guid, assets_relative_mesh_path)`` for the body's ``.mesh`` document."""
    target_source = document_guid.canonical(source_guid)
    target_asset = (
        document_guid.canonical(body_asset_guid) if document_guid.is_text(body_asset_guid) else None
    )
    for root, dirs, files in os.walk(layout.assets):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for filename in sorted(files):
            if not filename.lower().endswith(".mesh"):
                continue
            absolute = os.path.join(root, filename)
            try:
                with open(absolute, encoding="utf-8") as handle:
                    parsed = canonical_toml.loads(handle.read())
            except Exception:
                continue
            src = parsed.get("source")
            src_guid = src.get("guid") if isinstance(src, dict) else None
            if not document_guid.is_text(src_guid) or document_guid.canonical(src_guid) != target_source:
                continue
            try:
                asset_pair = mesh_document.asset_of(parsed)
            except ValueError:
                continue
            doc_asset = asset_pair[0] if asset_pair is not None else None
            if doc_asset != target_asset:
                continue
            meta = sidecar.read(sidecar.path_for(absolute))
            if meta is None:
                continue
            return meta.guid, layout.relative(absolute)
    return None


def find_placing_prefabs(
    layout: ProjectLayout,
    body_mesh_guid: str,
    body_mesh_rel: str,
    mesh_fields: schema.MeshFields,
) -> list[str]:
    """Every ``.prefab`` absolute path under ``assets/`` with a direct object placing the body mesh."""
    target_guid = document_guid.canonical(body_mesh_guid)
    found: list[str] = []
    for root, dirs, files in os.walk(layout.assets):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for filename in sorted(files):
            if not filename.lower().endswith(".prefab"):
                continue
            absolute = os.path.join(root, filename)
            try:
                with open(absolute, encoding="utf-8") as handle:
                    document = prefab.loads(handle.read(), absolute)
            except (OSError, prefab.PrefabDocumentError) as error:
                raise DetachError(f"Cannot read {layout.relative(absolute)}: {error}") from error
            if any(
                _places_mesh(entry, target_guid, body_mesh_rel, mesh_fields) is not None
                for entry in document.objects
            ):
                found.append(absolute)
    return found


def migrate_remainder_attribution(
    plan: DetachPlan, body_asset_guid: str, body_asset_name: str
) -> list[str]:
    """Stamp ``asset = { guid, name }`` onto whole-file reference docs and ``<blend>.meta`` parts."""
    if not plan.was_whole_file:
        return []
    canonical_guid = document_guid.canonical(body_asset_guid)
    touched: list[str] = []

    for root, dirs, files in os.walk(plan.layout.assets):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for filename in sorted(files):
            if not filename.lower().endswith(_MESH_REF_SUFFIXES):
                continue
            absolute = os.path.join(root, filename)
            try:
                with open(absolute, encoding="utf-8") as handle:
                    parsed = canonical_toml.loads(handle.read())
            except Exception:
                continue
            src = parsed.get("source")
            src_guid = src.get("guid") if isinstance(src, dict) else None
            if not document_guid.is_text(src_guid) or document_guid.canonical(src_guid) != plan.source_guid:
                continue
            if parsed.get("asset") is not None:
                continue
            reordered = _with_mesh_asset(parsed, canonical_guid, body_asset_name)
            atomic.write_text(absolute, canonical_toml.dumps(reordered))
            touched.append(absolute)

    meta_path = sidecar.path_for(plan.blend_path)
    with open(meta_path, encoding="utf-8") as handle:
        meta_table = canonical_toml.loads(handle.read())
    extract = meta_table.get("extract")
    parts = extract.get("parts") if isinstance(extract, dict) else None
    if isinstance(parts, list):
        updated_parts = []
        changed_meta = False
        for part in parts:
            if isinstance(part, dict):
                copied = dict(part)
                if copied.get("asset") is None:
                    copied["asset"] = canonical_guid
                    changed_meta = True
                updated_parts.append(canonical_toml.InlineTable(copied))
            else:
                updated_parts.append(part)
        if changed_meta:
            extract["parts"] = updated_parts
            atomic.write_text(meta_path, canonical_toml.dumps(meta_table))
            touched.append(meta_path)

    return touched


def stage_detached_meshes_and_placements(
    plan: DetachPlan,
    detached_models: list[tuple[str, str]],
) -> tuple[list[DetachedMesh], dict[str, list[tuple[str, str]]]]:
    """Write ``.mesh`` + ``.mesh.meta`` and direct ``StaticMesh`` placements BEFORE ``extract``."""
    detached_meshes: list[DetachedMesh] = []
    os.makedirs(plan.directories["meshes"], exist_ok=True)

    for name, raw_asset_guid in detached_models:
        asset_guid = document_guid.canonical(raw_asset_guid)
        mesh_guid = str(uuid.uuid4())
        mesh_abs = os.path.join(plan.directories["meshes"], f"{name}.mesh")
        mesh_rel = plan.layout.relative(mesh_abs)

        # Write the sidecar FIRST so a live watcher (if running) sees an already-identified file.
        meta_doc = {
            "schema_version": 1,
            "guid": mesh_guid,
            "importer": "mesh",
        }
        atomic.write_text(sidecar.path_for(mesh_abs), canonical_toml.dumps(meta_doc))

        mesh_doc = {
            "schema_version": 1,
            "source": canonical_toml.InlineTable({"guid": plan.source_guid, "path": plan.source_rel}),
            "asset": canonical_toml.InlineTable({"guid": asset_guid, "name": name}),
            "slot": "mesh",
        }
        atomic.write_text(mesh_abs, canonical_toml.dumps(mesh_doc))

        detached_meshes.append(
            DetachedMesh(
                name=name,
                asset_guid=asset_guid,
                mesh_guid=mesh_guid,
                mesh_abs=mesh_abs,
                mesh_rel=mesh_rel,
            )
        )

    staged_by_prefab: dict[str, list[tuple[str, str]]] = {}
    for prefab_path in plan.placing_prefabs:
        with open(prefab_path, encoding="utf-8") as handle:
            document = prefab.loads(handle.read(), prefab_path)

        added_for_doc: list[tuple[str, str]] = []
        holders = [
            (entry, match)
            for entry in document.objects
            if (match := _places_mesh(entry, plan.body_mesh_guid, plan.body_mesh_rel, plan.mesh_fields))
            is not None
        ]
        for holder, (mesh_comp, field_name) in holders:
            comp_id = mesh_comp.id
            comp_type = mesh_comp.type or plan.static_mesh_component.type_name
            for dm in detached_meshes:
                obj_guid = str(uuid.uuid4())
                placed = PrefabObject(
                    components=[
                        PrefabComponent(
                            well_known.META_ID,
                            well_known.META_TYPE,
                            {
                                well_known.GUID: obj_guid,
                                well_known.NAME: dm.name,
                                well_known.PARENT: holder.guid,
                            },
                        ),
                        PrefabComponent(
                            well_known.TRANSFORM_ID,
                            well_known.TRANSFORM_TYPE,
                            dict(new_prefab.IDENTITY_TRANSFORM),
                        ),
                        PrefabComponent(
                            comp_id,
                            comp_type,
                            {
                                field_name: canonical_toml.InlineTable(
                                    {"guid": dm.mesh_guid, "path": dm.mesh_rel}
                                )
                            },
                        ),
                        PrefabComponent(
                            mesh_slots.MATERIALS_ID,
                            mesh_slots.MATERIALS_TYPE,
                            {mesh_slots.SLOTS_FIELD: []},
                        ),
                    ]
                )
                document.objects.append(placed)
                added_for_doc.append((obj_guid, dm.mesh_rel))

        document.validate(prefab_path)
        atomic.write_text(prefab_path, prefab.dumps(document))
        staged_by_prefab[prefab_path] = added_for_doc

    return detached_meshes, staged_by_prefab


def finalize_placements(
    plan: DetachPlan,
    staged_by_prefab: dict[str, list[tuple[str, str]]],
) -> None:
    """Fill each detached placement's ``Slots`` and reconcile the body's after ``extract``."""
    for prefab_path, added_pairs in staged_by_prefab.items():
        with open(prefab_path, encoding="utf-8") as handle:
            document = prefab.loads(handle.read(), prefab_path)

        by_guid = document.by_guid()
        for obj_guid, detached_mesh_rel in added_pairs:
            entry = by_guid.get(obj_guid)
            if entry is None:
                continue
            ms = mesh_slots.model_slots_for(plan.layout, detached_mesh_rel)
            if ms is None or ms.missing_indices:
                raise DetachError(
                    f"Extraction left incomplete material bindings for '{detached_mesh_rel}'."
                )
            fresh = ms.fresh_slots()
            assert fresh is not None
            mat_comp = mesh_slots.materials_component(entry)
            if mat_comp is not None:
                mat_comp.data[mesh_slots.SLOTS_FIELD] = fresh

        for entry in document.objects:
            if _places_mesh(entry, plan.body_mesh_guid, plan.body_mesh_rel, plan.mesh_fields) is None:
                continue
            try:
                mesh_slots.reconcile_entry(
                    entry, plan.layout, plan.mesh_fields, label=entry.name or entry.guid or ""
                )
            except ValueError as error:
                raise DetachError(str(error)) from error

        document.validate(prefab_path)
        atomic.write_text(prefab_path, prefab.dumps(document))


def _places_mesh(
    entry: PrefabObject,
    target_guid: str,
    target_rel: str,
    mesh_fields: schema.MeshFields,
) -> tuple[PrefabComponent, str] | None:
    if entry.prefab is not None or entry.target is not None or entry.guid is None:
        return None
    found = schema.mesh_field(entry.components, mesh_fields)
    if found is None:
        return None
    component, field_name = found
    value = component.data.get(field_name)
    if isinstance(value, dict):
        raw_guid = value.get("guid")
        if document_guid.is_text(raw_guid) and document_guid.canonical(raw_guid) == target_guid:
            return component, field_name
        raw_path = value.get("path")
        if isinstance(raw_path, str) and os.path.normcase(raw_path) == os.path.normcase(target_rel):
            return component, field_name
    elif isinstance(value, str) and os.path.normcase(value) == os.path.normcase(target_rel):
        return component, field_name
    return None


def _with_mesh_asset(parsed: dict, asset_guid: str, asset_name: str) -> dict:
    """Rebuild ``parsed`` in ``MeshReferenceDocument.ToTable()`` key order with ``asset`` set."""
    out: dict = {}
    for key in ("schema_version", "source"):
        if key in parsed:
            val = parsed[key]
            out[key] = canonical_toml.InlineTable(val) if isinstance(val, dict) else val
    out["asset"] = canonical_toml.InlineTable({"guid": asset_guid, "name": asset_name})
    for key, val in parsed.items():
        if key in ("schema_version", "source", "asset"):
            continue
        out[key] = (
            canonical_toml.InlineTable(val)
            if isinstance(val, dict) and canonical_toml.is_written_inline(val)
            else val
        )
    return out
