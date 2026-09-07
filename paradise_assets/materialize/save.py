"""Blender objects -> prefab document.

Blender owns placement; the document owns component payloads, taken from the FILE (re-read at
save time) with the author's field overlay (:mod:`..edits`) applied on top, so a component this
addon has never heard of round-trips verbatim and a hand edit since the load survives. The save
refuses when the file's stamp moved (merging blind would drop whatever changed it), refuses a
parent that is not a document entity and guarantees ``meta`` + ``transform`` on every entity
(since v6 nothing downstream synthesizes placement), and writes atomically.

An object nobody moved keeps its authored numbers verbatim: the rebase round-trips to ~4e-8
relative, fine as a position and fatal as text because ``repr`` of a value moved in its last bit
is a different string, so an untouched scene would otherwise rewrite every transform. See
:func:`_unchanged`.

Override carriers (``meta.Target``) have no Blender object of their own: the load folds them into
the derived children it displays. They are document-owned data, like payloads, and travel
through :func:`_merge` untouched as long as the instance they belong to is still in the scene.
The edit they cannot express -- moving a derived child -- is refused rather than silently lost.
"""

from __future__ import annotations

import os
import uuid

import bpy
from mathutils import Matrix, Quaternion

from .. import edits as component_edits
from ..document import atomic, axes, canonical_toml, component_schema, overrides, project, well_known
from ..document import prefab as prefab_document
from ..document.asset_reference import AssetReference
from ..document.prefab import PrefabComponent, PrefabDocument, PrefabDocumentError, PrefabObject
from . import shapes, store, tagging
from .shapes import default_row as shapes_default_row

__all__ = ["SaveError", "SaveResult", "document_trs", "save_prefab"]

#: How close a Blender TRS has to be to the document's to count as untouched, relative to the
#: value's own magnitude. Round-tripping the rebase costs ~4e-8; float32 itself is ~1.2e-7. A
#: deliberate edit smaller than this is a sub-micron move on a metre-scale object, which no
#: viewport drag produces -- so the trade is "ignore an edit nobody can see" against "rewrite
#: every number in the file on every save".
_EPSILON = 1e-6


class SaveError(Exception):
    """The save could not proceed. The message is for the author."""


class SaveResult:
    """What a save changed, for the operator to report."""

    def __init__(self) -> None:
        self.written = 0
        self.moved = 0
        self.added = 0
        self.removed = 0
        self.edited = 0
        self.warnings: list[str] = []


def save_prefab(scene: bpy.types.Scene) -> SaveResult:
    """Write ``scene`` back to the document it was materialized from."""
    state = store.read_state(scene)
    if state is None:
        raise SaveError("this scene was not opened from a scene document")
    if not os.path.isfile(state.path):
        raise SaveError(f"the document is gone: {state.path}")
    if state.is_stale:
        raise SaveError(
            f"{os.path.basename(state.path)} changed on disk since it was opened. "
            "Reload it (Paradise Assets > Reload) -- saving now would discard that change."
        )

    with open(state.path, encoding="utf-8") as handle:
        base = prefab_document.loads(handle.read(), state.path)

    _adopt_new_groups(scene)
    _fold_parent_inverses(scene)
    _refuse_duplicate_identities(scene)
    _refuse_duplicate_derived(scene)
    _drop_widowed_derived(scene)
    _refuse_foreign_parents(scene)
    _refuse_rehomed_derived(scene)
    _refuse_orphaned_derived(scene)

    result = SaveResult()
    layout = project.locate(state.path)
    vocabulary = (
        component_schema.load(layout.root) if layout is not None
        else component_schema.Vocabulary({}, None))
    merged = _merge(scene, base, result, vocabulary)

    # What the reader will check, checked here: deleting the root (Blender unparents its
    # children) otherwise wrote a multi-root document that reported success and never loaded.
    try:
        merged.validate(state.path)
    except PrefabDocumentError as error:
        raise SaveError(
            f"{error}\n\nA document has exactly one root object. Parent the others beneath it, "
            "or reload to put the deleted root back."
        ) from error

    atomic.write_text(state.path, prefab_document.dumps(merged))
    store.write_state(scene, state.path)

    # Clear the overlay only AFTER the write: before it, a failed save loses the edits; kept, it
    # would re-apply on the next save over whatever someone else wrote meanwhile. The snapshot
    # is refreshed too, or add/remove vanish from the panel the moment the overlay clears.
    _refresh_snapshots(scene, merged, layout)
    for obj in _document_objects(scene) + _derived_objects(scene):
        component_edits.clear(obj)

    result.written = len(merged.objects)
    return result


def _refuse_duplicate_identities(scene: bpy.types.Scene) -> None:
    """Refuse when two objects claim one identity. Shift+D copies custom properties, guid
    included; writing both produced a document that would not load while the save reported
    success. Refused rather than re-minted because the addon cannot know which copy the
    author meant to keep."""
    seen: dict[str, str] = {}
    clashes: dict[str, list[str]] = {}

    for obj in _document_objects(scene):
        guid = store.guid_of(obj)
        if guid is None:
            continue
        if guid in seen:
            clashes.setdefault(guid, [seen[guid]]).append(obj.name)
        else:
            seen[guid] = obj.name

    if not clashes:
        return

    lines = [
        f"  '{names[0]}' and {', '.join(repr(n) for n in names[1:])} all claim {guid}"
        for guid, names in sorted(clashes.items())
    ]
    raise SaveError(
        "two objects share one identity, so the document would not load again:\n"
        + "\n".join(lines)
        + "\n\nA duplicated object keeps the original's identity. Delete the copy, or use "
        "Add Prefab Instance to place a genuinely new one."
    )


def _refuse_foreign_parents(scene: bpy.types.Scene) -> None:
    """Refuse a parent that is not a document entity. Saving it as a root instead silently
    moved the object, since Blender kept showing a hierarchy the document no longer had."""
    entities = {obj.name for obj in _document_objects(scene) if store.guid_of(obj) is not None}
    violations = [
        f"  '{obj.name}' is parented to '{obj.parent.name}'"
        for obj in _document_objects(scene)
        if obj.parent is not None and obj.parent.name not in entities
    ]

    if not violations:
        return

    raise SaveError(
        "a parent must be a document object too, or the game never sees the link:\n"
        + "\n".join(violations)
        + "\n\nParent these to a document object, or clear the parent (Alt+P, Clear and Keep "
        "Transform)."
    )


def _refuse_duplicate_derived(scene: bpy.types.Scene) -> None:
    """Refuse two objects standing for one prefab child. Shift+D copies custom properties, the
    derived marker and its ``(instance, local)`` address included, and a carrier is keyed on
    exactly that pair -- so the save would write one child's override from whichever copy the
    dictionary happened to keep."""
    seen: dict[tuple[str, str], str] = {}
    clashes: dict[tuple[str, str], list[str]] = {}

    for obj in _derived_objects(scene):
        address = store.local_of(obj)
        if address is None:
            continue
        key = (address[0], address[1])
        if key in seen:
            clashes.setdefault(key, [seen[key]]).append(obj.name)
        else:
            seen[key] = obj.name

    if not clashes:
        return

    lines = [f"  '{names[0]}' and {', '.join(repr(n) for n in names[1:])}" for names in clashes.values()]
    raise SaveError(
        "a prefab's child is in this scene twice, so its override would be written from one of "
        "them at random:\n" + "\n".join(lines)
        + "\n\nA copy of a prefab's child is not a new object -- the prefab decides what is in "
        "an instance. Delete the copy, or place another instance."
    )


def _refuse_rehomed_derived(scene: bpy.types.Scene) -> None:
    """Refuse a prefab child that was re-parented.

    The document cannot say it. ``resolve._rewrite_meta`` takes a resolved child's parent from
    the PREFAB's topology and ignores the carrier's own ``meta.Parent``, which addresses the
    instance rather than a parent -- so there is no field to write this into, and a save that
    went ahead would report success and the next load would put the child back.
    """
    moved = []
    for obj in _derived_objects(scene):
        stored = next(
            (c.get("data") for c in store.component_json(obj)
             if isinstance(c, dict) and str(c.get("id", "")).lower() == well_known.META_ID),
            None,
        )
        if not isinstance(stored, dict):
            continue
        was = stored.get(well_known.PARENT)
        now = store.guid_of(obj.parent) if obj.parent is not None else None
        if (was or None) != (now or None):
            moved.append(f"  '{obj.name}'")

    if not moved:
        return

    raise SaveError(
        "a prefab's child was re-parented, which this document cannot express:\n"
        + "\n".join(moved)
        + "\n\nA prefab decides its own hierarchy. Move the instance, or edit the prefab. "
        "Reload to put it back."
    )


def _drop_widowed_derived(scene: bpy.types.Scene) -> None:
    """Remove the display objects of an instance that is no longer here.

    Deleting an instance is an ordinary gesture, and Blender's Delete orphans its children rather
    than removing them. Those children are pure display -- the document has no entry for any of
    them -- so they are swept, not refused, and the instance's own removal is what the save
    reports. Without this, deleting an instance would trip the orphan refusal below.
    """
    live = {store.guid_of(obj) for obj in _document_objects(scene)}
    for obj in _derived_objects(scene):
        address = store.local_of(obj)
        if address is None or address[0] in live:
            continue
        bpy.data.objects.remove(obj, do_unlink=True)


def _refuse_orphaned_derived(scene: bpy.types.Scene) -> None:
    """Refuse a half-deleted prefab subtree.

    Blender's Delete orphans an object's children rather than deleting them, so removing a
    prefab child mid-tree leaves its descendants behind with no parent. Dropping the child in the
    document drops its descendants too (``resolve._drops_ancestor``), so the two views would
    disagree -- and the unparented survivors would be written as extra roots, which no reader
    accepts. Said here, where the cause can be named; an instance deleted whole was already
    swept by :func:`_drop_widowed_derived` and never reaches this.
    """
    stranded = [
        f"  '{obj.name}'" for obj in _derived_objects(scene) if obj.parent is None
    ]
    if not stranded:
        return

    raise SaveError(
        "a prefab's child was deleted but the objects under it were not:\n"
        + "\n".join(stranded)
        + "\n\nDeleting a prefab's child deletes everything under it. Delete the whole subtree, "
        "or reload to put it back."
    )


def _merge(
    scene: bpy.types.Scene, base: PrefabDocument, result: SaveResult, vocabulary
) -> PrefabDocument:
    """The document as Blender now has it, over the document as the file now has it. File order
    is kept: Blender guarantees no iteration order, and following it would reshuffle the file on
    every save. New objects follow, in name order.

    An instance's overrides ride along as CARRIERS, which have no object of their own: each is
    keyed ``(instance guid, prefab-local guid)`` -- the same key the resolver refuses duplicates
    on -- so an existing one is updated in its own slot rather than duplicated, and a new one is
    appended directly after the instance it belongs to. Nothing about that order comes from
    Blender, which is what lets an untouched scene rewrite zero bytes.
    """
    objects = {store.guid_of(obj): obj for obj in _document_objects(scene)}
    present = frozenset(objects)
    derived = {
        (address[0], address[1]): obj
        for obj in _derived_objects(scene)
        if (address := store.local_of(obj)) is not None
    }
    # Seeded from the FILE, not filled as pass 1 walks it: a carrier may sit anywhere in the
    # document, and one written after the instance it belongs to would otherwise be minted fresh
    # by `_new_carriers` and then emitted again when the walk reached it.
    placed = {
        (entry.parent, entry.target) for entry in base.objects if entry.target is not None
    }

    merged = PrefabDocument()
    for entry in base.objects:
        if entry.target is not None:
            # A carrier is the instance's data; it goes where the instance goes.
            if entry.parent not in present:
                result.removed += 1
                continue
            key = (entry.parent, entry.target)
            updated = _carrier_entry(entry, derived.get(key), objects.get(entry.parent), result)
            if updated is None:
                result.removed += 1
            else:
                merged.objects.append(updated)
            continue

        obj = objects.pop(entry.guid, None)
        if obj is None:
            result.removed += 1
            continue
        merged.objects.append(_object_entry(obj, entry, result, vocabulary))
        merged.objects.extend(_new_carriers(entry.guid, obj, derived, placed, result))

    for obj in sorted(objects.values(), key=_document_order):
        result.added += 1
        merged.objects.append(_object_entry(obj, None, result, vocabulary))
        merged.objects.extend(
            _new_carriers(store.guid_of(obj), obj, derived, placed, result))

    return merged


def _document_order(obj: bpy.types.Object) -> str:
    """What a new object is sorted by. The DOCUMENT's name, not Blender's: an Outliner mark is
    display, and sorting on it would order the file by whether something is an instance."""
    return store.document_name(obj) or obj.name


def _new_carriers(instance_guid, obj, derived, placed, result: SaveResult) -> list[PrefabObject]:
    """Carriers this instance needs and the file does not have yet, right after its own entry.

    Ordered by the prefab-local guid rather than by anything Blender knows: the next save finds
    them as existing carriers in exactly these slots, so the order is a fixed point and a
    second save of an unchanged scene rewrites nothing.
    """
    if store.prefab_of(obj) is None:
        return []

    made: list[PrefabObject] = []
    children = store.resolved_children(obj)
    wanted = sorted(
        {local for (instance, local) in derived if instance == instance_guid}
        | (children or set())
    )

    for local in wanted:
        key = (instance_guid, local)
        if key in placed:
            continue
        placed.add(key)
        child = derived.get(key)
        if child is None:
            # Materialized at load and gone now: the author deleted it. Only a local this
            # instance actually SHOWED can be dropped -- a child the prefab itself no longer has
            # was never in `children`, so a prefab edited elsewhere cannot look like a deletion.
            if children is None or local not in children:
                continue
            carrier = overrides.new_carrier(instance_guid, local)
            carrier.meta.data[well_known.DROPPED] = True
            result.edited += 1
            made.append(carrier)
            continue

        carrier = _carrier_entry(overrides.new_carrier(instance_guid, local), child, obj, result)
        if carrier is not None:
            made.append(carrier)
    return made


def _carrier_entry(
    original: PrefabObject, obj, instance, result: SaveResult
) -> PrefabObject | None:
    """One override carrier as Blender now has it, or ``None`` when it says nothing.

    Shaped exactly like :func:`_object_entry`: start from the file's entry and overwrite only
    what Blender owns, so a field nobody touched is written back byte-for-byte and a component
    this addon has never heard of rides along. ``obj`` is the resolved child in the scene, or
    ``None`` when the prefab no longer has it -- in which case the file's carrier is left alone
    rather than guessed at, and the resolver's own warning is what tells the author.
    """
    if obj is None:
        return original

    entry = original
    _write_carrier_transform(entry, obj, result)
    _apply_edits(obj, entry, result)
    return None if overrides.is_empty(entry) else entry


def _write_carrier_transform(entry: PrefabObject, obj, result: SaveResult) -> None:
    """Where this child stands, if that is not where the prefab and the file already put it.

    Measured against the RESOLVED snapshot -- what the load displayed -- and not against the
    carrier's own payload, which is a PARTIAL: ``resolve._merge_data`` merges per field, so a
    carrier holding only ``Position`` inherits rotation and scale from the prefab. Feeding that
    partial to :func:`_unchanged` reads the absent keys as the IDENTITY, calls every untouched
    child moved, and rewrites all three channels on every save of a file nobody edited.
    """
    position, rotation, scale = _document_trs(obj)
    shown = _snapshot_data(obj, well_known.TRANSFORM_ID)
    if _unchanged(shown, (position, rotation, scale)):
        return

    component = entry.component(well_known.TRANSFORM_ID)
    base = _base_data(obj, well_known.TRANSFORM_ID)
    if _unchanged(base, (position, rotation, scale)):
        # Put back where the prefab has it: the override is not smaller, it is gone.
        if component is not None:
            entry.components.remove(component)
            result.edited += 1
        return

    if component is None:
        component = PrefabComponent(well_known.TRANSFORM_ID, well_known.TRANSFORM_TYPE, {})
        entry.components.append(component)

    # All three channels, never only the one that moved: a partial override silently inherits
    # the rest from a prefab somebody else is still editing.
    component.data[well_known.POSITION] = [float(v) for v in position]
    component.data[well_known.ROTATION] = [float(v) for v in rotation]
    component.data[well_known.SCALE] = [float(v) for v in scale]
    result.moved += 1


def _snapshot_data(obj, component_id: str) -> dict:
    """One component's payload as the load DISPLAYED it (prefab plus overrides)."""
    return _payload_of(store.component_json(obj), component_id)


def _base_data(obj, component_id: str) -> dict:
    """One component's payload as the PREFAB alone says it."""
    return _payload_of(store.base_json(obj), component_id)


def _payload_of(components: list, component_id: str) -> dict:
    found = next(
        (c.get("data") for c in components
         if isinstance(c, dict) and str(c.get("id", "")).lower() == component_id.lower()),
        None,
    )
    return found if isinstance(found, dict) else {}


def _adopt_new_groups(scene: bpy.types.Scene) -> None:
    """Give an Empty the author made to group document objects under an identity of its own.

    The gesture is "add an Empty, parent things to it": that Empty has no GUID, so without this
    it is neither written nor a legal parent and the save refuses. Only an EMPTY holding at
    least one document object qualifies -- a stray camera or light stays Blender's own, and an
    Empty with nothing in it is a marker somebody has not finished. One left unparented hangs
    off the document root, exactly as a placed instance does: a second root never loads.

    Never over the ROOT: the root IS the document, and adopting an Empty it was dragged under
    would mint a new root and write a document that is no longer the one that was opened. With
    no unique parentless root there is nothing to adopt into, and the foreign-parent rule names
    the Empty instead.

    Minted as ``uuid4`` rather than waited for: sidecars mint identities for FILES under
    ``assets/``, and an object inside a document has never been one of those.
    """
    root = _root_object(scene)
    if root is None:
        return
    # World matrices are stale until the depsgraph runs; parenting from a stale one moved the
    # Empty to the origin on the first save.
    bpy.context.view_layer.update()
    adopted = True
    while adopted:
        adopted = False
        for obj in scene.collection.all_objects:
            if obj.type != "EMPTY" or store.guid_of(obj) is not None or shapes.is_shape(obj):
                # A collision-shape Empty is its owner's handle, never a group: adopted, it would
                # be written twice, as a group object and as a shape row on the same Empty. Left
                # alone, the foreign-parent rule names it.
                continue
            if not any(store.guid_of(child) is not None for child in obj.children):
                continue
            store.tag_object(obj, str(uuid.uuid4()), [])
            store.tag_name(obj, obj.name)
            adopted = True
            if obj.parent is None:
                world = obj.matrix_world.copy()
                obj.parent = root
                obj.matrix_parent_inverse.identity()
                obj.matrix_world = world


def _fold_parent_inverses(scene: bpy.types.Scene) -> None:
    """Move a non-identity ``matrix_parent_inverse`` into the local channels.

    Ctrl+P and the Outliner's drag-to-parent keep an object in place by storing the offset in
    the parent inverse, and the document has no field for it: the channels alone would put the
    object somewhere else on the next load. Folded here, once, rather than read through on every
    save -- a decomposition is lossy, and the channels are what :func:`_unchanged` compares.
    """
    handles = [obj for obj in scene.collection.all_objects if shapes.is_shape(obj)]
    # Derived children too: they are transform sources now (a move becomes a carrier), and an
    # inverse left folded away would put the carrier's numbers somewhere the viewport never was.
    for obj in _document_objects(scene) + _derived_objects(scene) + handles:
        if obj.parent is None or obj.matrix_parent_inverse == _IDENTITY:
            continue
        local = obj.matrix_parent_inverse @ obj.matrix_basis
        obj.matrix_parent_inverse.identity()
        obj.matrix_basis = local


_IDENTITY = Matrix.Identity(4)


def _root_object(scene: bpy.types.Scene):
    """The document root, or ``None`` when it is not unique -- the root rule then refuses the
    save and names the objects, which is a better error than anything this could invent."""
    found = [obj for obj in _document_objects(scene) if obj.parent is None]
    return found[0] if len(found) == 1 else None


def _object_entry(
    obj: bpy.types.Object, original: PrefabObject | None, result: SaveResult, vocabulary
) -> PrefabObject:
    """One Blender object as a document object: the file's entry with only what Blender owns
    overwritten, which is what keeps an instance an instance rather than the plain objects it
    displays as."""
    guid = store.guid_of(obj)
    entry = PrefabObject() if original is None else original

    # Only a NEW instance carries its own prefab reference; an existing entry keeps the file's,
    # so a stale marker cannot override an edited document.
    if original is None and store.prefab_of(obj) is not None:
        reference_guid, reference_path = store.prefab_of(obj)
        entry.prefab = AssetReference(reference_guid, reference_path)

    parent_guid = store.guid_of(obj.parent) if obj.parent is not None else None
    _write_meta(entry, guid, store.document_name(obj), parent_guid)
    _write_transform(entry, obj, original, result)

    # Last, so an overlay edit could never win against the meta/transform writes above.
    _apply_edits(obj, entry, result)
    # After the overlay: a typed IsTrigger and a moved Empty land on the same row. ``entry`` is
    # the file's own entry, so for an instance only the lists IT authors are baked.
    result.edited += shapes.bake(obj, entry, vocabulary, _default_row)
    return entry


def _default_row(field) -> dict:
    return shapes_default_row(field)


def _apply_edits(obj: bpy.types.Object, entry: PrefabObject, result: SaveResult) -> None:
    """Apply pending add/remove/revert, then field edits.

    An edit to a component the object's own entry does not author is an OVERRIDE: the component
    is created on ``entry`` carrying only the fields that were touched. Only those -- copying the
    prefab's other fields in would freeze them, and a frozen field shadows the prefab forever,
    which is the failure `extract` goes out of its way to avoid. ``entry`` is the instance's own
    file entry for an instance, and the child's carrier for a resolved child, so one function
    serves both.
    """
    _apply_structure(obj, entry, result)
    _apply_reverts(obj, entry, result)

    pending = component_edits.read(obj)
    if not pending:
        return

    own = {cid: fields for cid, fields in pending.items() if store.authors(obj, cid)}
    inherited = {cid: fields for cid, fields in pending.items() if cid not in own}

    missing = [cid for cid in own if entry.component(cid) is None]
    result.edited += component_edits.apply_to(entry, own)
    for component_id in missing:
        result.warnings.append(
            f"{obj.name}: an edit to component {component_id} was dropped -- the document no "
            "longer carries it.")

    for component_id, fields in inherited.items():
        component = entry.component(component_id)
        if component is None:
            component = PrefabComponent(component_id, _shown_type(obj, component_id), {})
            entry.components.append(component)
        result.edited += component_edits.apply_to(entry, {component_id: fields})


def _shown_type(obj: bpy.types.Object, component_id: str) -> str | None:
    """The CLR type name the prefab gives this component, so the resolver's
    ``component.type or overriding.type`` never has to choose between two spellings."""
    for component in store.component_json(obj):
        if isinstance(component, dict) and str(component.get("id", "")).lower() == component_id.lower():
            name = component.get("type")
            return name if isinstance(name, str) else None
    return None


def _apply_reverts(obj: bpy.types.Object, entry: PrefabObject, result: SaveResult) -> None:
    """Drop overridden fields the author asked to hand back to the prefab.

    Not the same as forgetting a pending edit: once an override is IN the file, forgetting the
    overlay changes nothing, and "revert" would silently do nothing in the one case an author is
    actually in. A component left with no fields of its own is removed with them.
    """
    reverted = component_edits.reverted_paths(obj)
    if not reverted:
        return

    for component_id, paths in reverted.items():
        component = entry.component(component_id)
        if component is None:
            continue
        for path in paths:
            if component_edits.drop_path(component.data, path):
                result.edited += 1
        if not component.data and component.id not in (
            well_known.META_ID, well_known.TRANSFORM_ID
        ):
            entry.components.remove(component)


def _apply_structure(obj: bpy.types.Object, entry: PrefabObject, result: SaveResult) -> None:
    """Insert added components and drop removed ones. meta / transform cannot be removed."""
    removed = {item.lower() for item in component_edits.removed_ids(obj)}
    added = component_edits.added_components(obj)
    if not removed and not added:
        return

    owned = {well_known.META_ID.lower(), well_known.TRANSFORM_ID.lower()}
    if removed:
        kept = []
        for component in entry.components:
            if component.id.lower() in removed and component.id.lower() not in owned:
                result.edited += 1
                continue
            kept.append(component)
        entry.components = kept

        # A component the entry never carried is the PREFAB's, and deleting it from the entry
        # says nothing. `removed = true` is how the format hides one, and the payload has to be
        # empty -- the reader refuses "removed, and here is its content".
        for component_id in component_edits.removed_ids(obj):
            if component_id.lower() in owned or store.authors(obj, component_id):
                continue
            if entry.component(component_id) is None:
                entry.components.append(PrefabComponent(component_id, None, {}, removed=True))
                result.edited += 1

    present = {component.id.lower() for component in entry.components}
    for spec in added:
        component_id = str(spec.get("id", ""))
        if not component_id or component_id.lower() in present or component_id.lower() in owned:
            continue
        data = spec.get("data")
        entry.components.append(PrefabComponent(
            component_id,
            spec.get("type"),
            canonical_toml.restore_inline_tables(dict(data)) if isinstance(data, dict) else {},
        ))
        present.add(component_id.lower())
        result.edited += 1


def _refresh_snapshots(scene: bpy.types.Scene, merged: PrefabDocument, layout) -> None:
    """Re-tag every object from the document that was just written.

    RESOLVED, not the raw entries. An instance's entry holds meta, transform and its overrides;
    the object has to keep showing the prefab's components folded in, or saving a level collapses
    every instance's Components panel to two rows until the next reload. It also refreshes the
    baselines a carrier's move detection is measured against, without which the second save
    disagrees with the first. Same function the load tags through, so the two cannot drift.
    """
    if layout is None:
        return
    resolution = tagging.resolve_document(merged, layout)
    by_guid = {entry.guid.lower(): entry for entry in resolution.document.objects if entry.guid}

    for obj in _document_objects(scene) + _derived_objects(scene):
        guid = store.guid_of(obj)
        if not guid or guid.lower() not in by_guid:
            continue
        tagging.tag(obj, by_guid[guid.lower()], resolution)


def _write_meta(entry: PrefabObject, guid: str, name: str | None, parent: str | None) -> None:
    """Update identity, name and parent in place, leaving any other meta field alone. ``None``
    for the name means "the author did not touch it": an authored name stays, and an object
    that never had one gains none."""
    component = entry.component(well_known.META_ID)
    if component is None:
        component = PrefabComponent(well_known.META_ID, well_known.META_TYPE, {})
        entry.components.insert(0, component)

    component.data[well_known.GUID] = guid
    if name is not None:
        component.data[well_known.NAME] = name
    if parent is None:
        component.data.pop(well_known.PARENT, None)
    else:
        component.data[well_known.PARENT] = parent


def _write_transform(
    entry: PrefabObject, obj: bpy.types.Object, original: PrefabObject | None, result: SaveResult
) -> None:
    position, rotation, scale = _document_trs(obj)

    stored = original.component(well_known.TRANSFORM_ID) if original is not None else None
    if stored is not None:
        if _unchanged(stored.data, (position, rotation, scale)):
            return   # untouched: keep the authored numbers verbatim
        result.moved += 1
    elif original is not None:
        # The document authored NO transform and the object still stands at identity: keep it
        # absent. Absence is meaningful to a game ("no transform component, no transform" is how
        # ShiningPie tells a camera or a light from a placed thing), and the engine's extractor
        # writes model prefabs without one; a save that changes nothing rewrites nothing.
        if _unchanged({}, (position, rotation, scale)):
            return
        result.moved += 1

    # A NEW object, or one moved off identity, gets a transform: nothing downstream synthesizes
    # placement.

    component = entry.component(well_known.TRANSFORM_ID)
    if component is None:
        component = PrefabComponent(well_known.TRANSFORM_ID, well_known.TRANSFORM_TYPE, {})
        entry.components.append(component)

    component.data[well_known.POSITION] = [float(v) for v in position]
    component.data[well_known.ROTATION] = [float(v) for v in rotation]
    component.data[well_known.SCALE] = [float(v) for v in scale]


def document_trs(obj: bpy.types.Object):
    """Where ``obj`` stands, in the document's axes -- the one conversion the save writes and
    the panel shows."""
    return axes.from_blender_trs(*_blender_trs(obj))


_document_trs = document_trs


def _blender_trs(obj: bpy.types.Object):
    """Local TRS from the channels, not ``matrix_basis``: a matrix decomposition is lossy. The
    rotation is read from whichever channel the mode makes live; an ``AXIS_ANGLE`` object read
    through ``rotation_euler`` saved a rotation the viewport never showed (#37). ``w >= 0``,
    since q and -q are one rotation and the file should spell it one way."""
    mode = obj.rotation_mode
    if mode == "QUATERNION":
        rotation = obj.rotation_quaternion.copy()
    elif mode == "AXIS_ANGLE":
        angle, x, y, z = obj.rotation_axis_angle
        rotation = Quaternion((x, y, z), angle)
    else:
        rotation = obj.rotation_euler.to_quaternion()
    if rotation.w < 0.0:
        rotation.negate()
    w, x, y, z = rotation
    return (
        tuple(float(v) for v in obj.location),
        (float(x), float(y), float(z), float(w)),
        tuple(float(v) for v in obj.scale),
    )


def _unchanged(stored: dict, computed) -> bool:
    """Whether the object still sits where the document put it. Relative to the stored
    magnitude (400 m and 0.01 scale do not deserve the same slack); rotations by dot product,
    since q and -q are one rotation."""
    # Sequence, not `list`: the identity defaults are tuples, and testing `list` alone made
    # every transform-less object gain a transform on the first save.
    def numbers(key, count, default):
        value = stored.get(key, default)
        if not isinstance(value, (list, tuple)) or len(value) != count:
            return None
        return [float(v) for v in value]

    for key, index, default in (
        (well_known.POSITION, 0, (0.0, 0.0, 0.0)),
        (well_known.SCALE, 2, (1.0, 1.0, 1.0)),
    ):
        authored = numbers(key, 3, default)
        if authored is None:
            return False
        for x, y in zip(authored, computed[index], strict=True):
            if abs(x - y) > _EPSILON * max(abs(x), 1.0):
                return False

    rotation = numbers(well_known.ROTATION, 4, (0.0, 0.0, 0.0, 1.0))
    if rotation is None:
        return False
    dot = abs(sum(x * y for x, y in zip(rotation, computed[1], strict=True)))
    return abs(1.0 - dot) <= _EPSILON


def _derived_objects(scene: bpy.types.Scene) -> list[bpy.types.Object]:
    """Objects a prefab resolved into the scene. They are written only as overrides on the
    instance they belong to, never as entries of their own, which is why they are kept strictly
    out of :func:`_document_objects` rather than filtered back out downstream."""
    return [
        obj for obj in scene.collection.all_objects
        if store.guid_of(obj) is not None and store.is_derived(obj)
    ]


def _document_objects(scene: bpy.types.Scene) -> list[bpy.types.Object]:
    """The objects this save may write. Derived (prefab-resolved) objects are excluded, or the
    merge would flatten every instance on the first save."""
    return [
        obj for obj in scene.collection.all_objects
        if store.guid_of(obj) is not None and not store.is_derived(obj)
    ]
