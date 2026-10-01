"""Prepare selection snapshots without exporting geometry already owned by an asset.

Only raw meshes go to the model exporter. Documents and references remain authoritative;
Blender contributes names, selection and live placement, never cached component payloads.
"""

from __future__ import annotations

import copy
import math
import os
import uuid
from dataclasses import dataclass

from .. import edits
from ..document import assets, axes, guid, new_prefab, overrides, prefab, resolve, schema, well_known
from ..document.asset_reference import AssetReference
from ..document.new_prefab import CreateError
from ..document.prefab import PrefabComponent, PrefabDocument, PrefabObject


@dataclass(frozen=True)
class SelectionPlan:
    raw: list
    document: PrefabDocument
    origin: object
    has_references: bool


def can_select(obj) -> bool:
    """Cheap UI eligibility only; preparation performs authoritative validation."""
    from . import store

    if obj is None:
        return False
    if obj.get(store.PREFAB_KEY):
        return True
    if store.is_derived(obj):
        # A selected owner's displayed children are consumed by that owner, not exported.
        return True
    if store.guid_of(obj):
        if obj.type == "MESH" or getattr(obj, "instance_type", None) == "COLLECTION":
            return True
        fields = schema.MeshFields(None, None)
        return any(
            fields.is_mesh_field(component.get("type"), field,
                                 value.get("path") if isinstance(value, dict) else value)
            for component in store.component_json(obj)
            if isinstance(component, dict) and isinstance(component.get("data"), dict)
            for field, value in component["data"].items()
        )
    return obj.type == "MESH" and not any(mod.type == "ARMATURE" for mod in obj.modifiers)


def prepare(context, layout, name: str) -> SelectionPlan:
    """Snapshot references and partition raw meshes. No scene or file writes are made.

    Selected reference parents are retained. Across raw/unselected parents placement is
    rebased to the new root, since the model exporter bakes raw objects into world space.
    A derived child alone is not an independent asset: select its owning instance instead.
    """
    from . import store

    members = list(context.selected_objects)
    if context.mode != "OBJECT" or not members:
        raise CreateError("Select static meshes or existing asset references in Object Mode first.")
    active = context.active_object if context.active_object in members else members[0]
    origin = active.matrix_world.translation.copy()
    document = new_prefab.root_only(name)
    raw, selected = [], []
    source = PrefabDocument()
    needs_source = any(store.guid_of(obj) or obj.get(store.PREFAB_KEY) for obj in members)
    if needs_source:
        state = store.read_state(context.scene)
        if state is not None:
            if state.is_stale:
                raise CreateError("The current document changed on disk. Reload it before creating a prefab.")
            source = _read_document(state.path)
    by_guid = source.by_guid()
    fields = schema.load(layout.root) if needs_source else None
    for obj in members:
        if store.is_derived(obj):
            continue
        identity = store.guid_of(obj)
        marker = _marker(obj)
        if identity is not None and not guid.is_text(identity):
            raise CreateError(f"'{obj.name}' has a stale document identity. Reload the document.")
        original = by_guid.get(identity)
        if original is not None and original.target is not None:
            raise CreateError(f"'{obj.name}' is an override carrier, not an independent object.")
        if original is not None:
            if marker is not None and marker != original.prefab:
                raise CreateError(f"'{obj.name}' has a stale prefab reference. Reload the document.")
            entry = copy.deepcopy(original)
        elif marker is not None:
            entry = PrefabObject.with_meta(identity or str(uuid.uuid4()))
            entry.prefab = marker
        elif identity is not None or any(key in obj for key in (store.GUID_KEY, store.PREFAB_KEY, store.COMPONENTS_KEY)):
            raise CreateError(f"'{obj.name}' is not in the current document. Reload it; its mesh will not be exported.")
        else:
            _refuse_model_source(obj)
            if obj.type != "MESH" or any(mod.type == "ARMATURE" for mod in obj.modifiers):
                raise CreateError(f"'{obj.name}' is not a static mesh. Select only unrigged mesh objects.")
            raw.append(obj)
            continue
        _refuse_edits(obj)
        if entry.prefab is None and schema.mesh_field(entry.components, fields) is None:
            raise CreateError(f"'{obj.name}' is not a prefab instance or a mesh-bearing document object.")
        selected.append((obj, entry))
    identities = [entry.guid for _, entry in selected]
    if len(identities) != len(set(identities)):
        raise CreateError("Selected document objects share an identity. Save or reload the document first.")

    owners = {store.guid_of(obj): obj for obj, entry in selected if entry.prefab is not None}
    for obj in members:
        if not store.is_derived(obj):
            continue
        local = store.local_of(obj)
        if local is None or local[0] not in owners:
            raise CreateError(f"'{obj.name}' is a prefab child. Select its owning instance instead.")

    # Resolve only the selected references: an unrelated broken level object is not copied.
    checking = PrefabDocument([copy.deepcopy(entry) for _, entry in selected])
    checking.objects.extend(copy.deepcopy(entry) for entry in source.objects
                            if entry.target is not None and entry.parent in owners)
    reader = _reference_reader(layout, fields)
    try:
        expanded = resolve.resolve(checking, reader)
        if expanded.errors:
            raise CreateError("Cannot copy asset references: " + "; ".join(expanded.errors))
        locals_map = overrides.locals_of(checking, reader)
    except (ValueError, RecursionError) as error:
        raise CreateError(f"Cannot resolve selected asset references: {error}") from error
    for entry in expanded.document.objects:
        _validate_components(entry, layout, fields)

    ids = {id(obj): str(uuid.uuid4()) for obj, _ in selected}
    old_ids = {entry.guid: ids[id(obj)] for obj, entry in selected}
    scene_objects = list(context.scene.objects)
    for obj, entry in selected:
        if entry.prefab is not None:
            _instance_children(obj, entry, source, expanded.document, locals_map,
                               scene_objects, document, ids[id(obj)])
        parent = obj.parent
        seen = {id(obj)}
        while parent is not None and id(parent) not in ids:
            if id(parent) in seen:
                raise CreateError(f"'{obj.name}' has a cyclic hierarchy.")
            seen.add(id(parent))
            parent = parent.parent
        if parent is obj:
            raise CreateError(f"'{obj.name}' has a cyclic hierarchy.")
        matrix = _relative_matrix(obj, parent, origin)
        _set_meta(entry, ids[id(obj)], store.document_name(obj),
                  ids[id(parent)] if parent is not None else document.root_guid)
        _set_transform(entry, matrix)
        document.objects.append(entry)
    # Preserve saved overrides for children not materialized by Blender (newly placed instances).
    existing = {(entry.parent, entry.target) for entry in document.objects if entry.target}
    for carrier in source.objects:
        if carrier.target is None or carrier.parent not in old_ids:
            continue
        owner = old_ids[carrier.parent]
        if (owner, carrier.target) not in existing:
            cloned = copy.deepcopy(carrier)
            cloned.meta.data[well_known.PARENT] = owner
            if well_known.GUID in cloned.meta.data:
                cloned.meta.data[well_known.GUID] = str(uuid.uuid4())
            document.objects.append(cloned)
    document = _validated(document, "selection snapshot")
    return SelectionPlan(raw, document, origin, bool(selected))


def compose(plan: SelectionPlan, seed: PrefabDocument | None) -> PrefabDocument:
    """Attach the baked raw seed under the identity root, without mutating either input."""
    if not plan.has_references:
        if seed is None:
            raise CreateError("Raw geometry requires an extracted prefab seed.")
        return seed
    if plan.raw and seed is None:
        raise CreateError("The mixed selection requires an extracted prefab seed for its raw meshes.")
    result = copy.deepcopy(plan.document)
    if seed is not None:
        cloned = copy.deepcopy(seed)
        mapping = {entry.guid: str(uuid.uuid4()) for entry in cloned.objects if entry.guid}
        root = cloned.root()
        for entry in cloned.objects:
            if entry.meta is None:
                raise CreateError("The extracted prefab has an object without identity metadata.")
            if entry.guid:
                entry.meta.data[well_known.GUID] = mapping[entry.guid]
            if entry is root:
                entry.meta.data[well_known.PARENT] = result.root_guid
            elif entry.parent:
                entry.meta.data[well_known.PARENT] = mapping.get(entry.parent, entry.parent)
        result.objects.extend(cloned.objects)
    return _validated(result, "composed selection")


def _validated(document, source):
    try:
        return prefab.loads(prefab.dumps(document), source)
    except (ValueError, prefab.PrefabDocumentError) as error:
        raise CreateError(f"Cannot create prefab: {error}") from error


def _refuse_model_source(obj):
    from . import meshes

    collections = list(getattr(obj, "users_collection", ()))
    collections.append(getattr(obj, "instance_collection", None))
    if (getattr(obj, "library", None) is not None
            or getattr(getattr(obj, "data", None), "library", None) is not None
            or any(collection is not None and
                   (meshes.model_of(collection) is not None or meshes.SOURCE_KEY in collection)
                   for collection in collections)):
        raise CreateError(
            f"'{obj.name}' belongs to an existing model source, not raw geometry. "
            "Place its canonical prefab or mesh document before creating this prefab."
        )


def _marker(obj) -> AssetReference | None:
    from . import store

    if store.PREFAB_KEY not in obj:
        return None
    try:
        marker = store.prefab_of(obj)
    except (TypeError, AttributeError, ValueError) as error:
        raise CreateError(f"'{obj.name}' has a broken prefab marker. Reload the document.") from error
    if marker is None or not guid.is_text(marker[0]) or not isinstance(marker[1], str):
        raise CreateError(f"'{obj.name}' has a broken prefab marker. Reload the document.")
    return AssetReference(guid.canonical(marker[0]), marker[1])


def _read_document(path: str) -> PrefabDocument:
    try:
        with open(path, encoding="utf-8") as handle:
            return prefab.loads(handle.read(), path)
    except (OSError, prefab.PrefabDocumentError) as error:
        raise CreateError(f"Cannot read reference document: {error}") from error


def _asset_path(layout, path: str, identity: str | None = None) -> str:
    if not isinstance(path, str) or not path:
        raise CreateError("An asset reference has no path. Repair it before creating a prefab.")
    absolute = os.path.realpath(layout.resolve(path))
    try:
        inside = os.path.commonpath((absolute, os.path.realpath(layout.assets))) == os.path.realpath(layout.assets)
    except ValueError:
        inside = False
    if not inside or not os.path.isfile(absolute):
        raise CreateError(f"Asset reference '{path}' is missing or outside the project. Repair it first.")
    if identity is not None:
        if not guid.is_text(identity) or assets.read_sidecar_guid(absolute + ".meta") != guid.canonical(identity):
            raise CreateError(f"Asset reference '{path}' has a missing or mismatched identity. Repair it first.")
    return absolute


def _reference_reader(layout, fields):
    cache = {}

    def read(reference):
        path = _asset_path(layout, reference.path, reference.guid)
        if not path.lower().endswith(".prefab"):
            raise CreateError(f"'{reference.path}' is not a prefab reference.")
        if path not in cache:
            document = _read_document(path)
            for entry in document.objects:
                _validate_components(entry, layout, fields)
            cache[path] = document
        return cache[path]

    return read


def _validate_components(entry, layout, fields):
    def visit(value):
        if isinstance(value, dict):
            if "guid" in value and "path" in value:
                _asset_path(layout, value["path"], value["guid"])
            else:
                for child in value.values():
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for component in entry.components:
        visit(component.data)
        for field, value in component.data.items():
            path = value.get("path") if isinstance(value, dict) else value
            if fields is not None and fields.is_mesh_field(component.type, field, path):
                _asset_path(layout, path)


def _refuse_edits(obj):
    if edits.count(obj):
        raise CreateError(f"'{obj.name}' has unsaved component edits. Save the document first.")


def _relative_matrix(obj, parent, origin):
    if parent is not None:
        try:
            return parent.matrix_world.inverted() @ obj.matrix_world
        except ValueError as error:
            raise CreateError(f"'{obj.name}' has a singular parent transform.") from error
    matrix = obj.matrix_world.copy()
    matrix.translation -= origin
    return matrix


def _set_meta(entry, identity, name, parent):
    if entry.meta is None:
        entry.components.insert(0, PrefabComponent(well_known.META_ID, well_known.META_TYPE))
    data = entry.meta.data
    for key in (well_known.GUID, well_known.NAME, well_known.PARENT, well_known.TARGET, well_known.DROPPED):
        data.pop(key, None)
    data[well_known.GUID] = identity
    if name is not None:
        data[well_known.NAME] = name
    data[well_known.PARENT] = parent


def _set_transform(entry, matrix):
    converted = axes.to_document(matrix)
    if not all(math.isfinite(value) for row in converted for value in row):
        raise CreateError("A referenced object has a non-finite transform.")
    position, rotation, scale = axes._decompose(converted)
    rebuilt = axes.trs_to_matrix(position, rotation, scale)
    if any(abs(converted[r][c] - rebuilt[r][c]) > 1e-5 * max(1.0, abs(converted[r][c]))
           for r in range(4) for c in range(4)):
        raise CreateError("A referenced object has shear that a prefab transform cannot represent. Fix its hierarchy first.")
    component = entry.component(well_known.TRANSFORM_ID)
    if component is None:
        component = PrefabComponent(well_known.TRANSFORM_ID, well_known.TRANSFORM_TYPE)
        entry.components.append(component)
    component.removed = False
    component.data = {well_known.POSITION: list(position), well_known.ROTATION: list(rotation),
                      well_known.SCALE: list(scale)}


def _instance_children(obj, entry, source, expanded, locals_map, scene_objects, document, new_id):
    from . import store

    expected = {identity: local for identity, local in locals_map.items() if local.instance == entry.guid}
    live = {}
    by_guid = expanded.by_guid()
    for child in scene_objects:
        local = store.local_of(child) if store.is_derived(child) else None
        if local is None or local[0] != entry.guid:
            continue
        identity = store.guid_of(child)
        if identity not in expected or identity in live or local[1] != expected[identity].local:
            raise CreateError(f"'{child.name}' has stale or duplicate prefab-child identity. Reload the document.")
        live[identity] = child
    materialized = store.resolved_children(obj) or set()
    if materialized - {expected[identity].local for identity in live}:
        raise CreateError(f"'{obj.name}' has missing prefab children. Save or reload the document first.")
    for identity, child in live.items():
        _refuse_edits(child)
        original = by_guid.get(identity)
        if original is None or child.parent is None or store.guid_of(child.parent) != original.parent:
            raise CreateError(f"'{child.name}' was reparented inside a prefab. Save or reload it first.")
        local = expected[identity]
        saved = next((item for item in source.objects
                      if item.target == local.local and item.parent == entry.guid), None)
        carrier = copy.deepcopy(saved) if saved is not None else overrides.new_carrier(new_id, local.local)
        carrier.meta.data[well_known.PARENT] = new_id
        if well_known.GUID in carrier.meta.data:
            carrier.meta.data[well_known.GUID] = str(uuid.uuid4())
        name = store.document_name(child)
        if name != original.name:
            carrier.meta.data[well_known.NAME] = name or ""
        transformed = PrefabObject()
        _set_transform(transformed, _relative_matrix(child, child.parent, None))
        current = transformed.component(well_known.TRANSFORM_ID)
        authored = original.component(well_known.TRANSFORM_ID)
        baseline = dict(new_prefab.IDENTITY_TRANSFORM)
        if authored is not None:
            baseline.update(authored.data)
        expected_matrix = axes.trs_to_matrix(
            baseline[well_known.POSITION], baseline[well_known.ROTATION], baseline[well_known.SCALE])
        actual_matrix = axes.trs_to_matrix(
            current.data[well_known.POSITION], current.data[well_known.ROTATION], current.data[well_known.SCALE])
        if any(abs(expected_matrix[r][c] - actual_matrix[r][c]) > 1e-5 * max(1.0, abs(expected_matrix[r][c]))
               for r in range(4) for c in range(4)):
            carrier.components = [component for component in carrier.components
                                  if component.id != well_known.TRANSFORM_ID]
            carrier.components.append(current)
        if not overrides.is_empty(carrier):
            document.objects.append(carrier)
