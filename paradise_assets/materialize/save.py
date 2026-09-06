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
from mathutils import Quaternion

from .. import edits as component_edits
from ..document import atomic, axes, canonical_toml, well_known
from ..document import prefab as prefab_document
from ..document.asset_reference import AssetReference
from ..document.prefab import PrefabComponent, PrefabDocument, PrefabDocumentError, PrefabObject
from . import groups, store
from .meshes import LIBRARY_COLLECTION

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

    _refuse_duplicate_identities(scene)
    _refuse_foreign_parents(scene)
    _refuse_moved_derived(scene)

    result = SaveResult()
    merged = _merge(scene, base, result)

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
    _refresh_snapshots(scene, merged)
    for obj in _document_objects(scene):
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


def _refuse_moved_derived(scene: bpy.types.Scene) -> None:
    """Refuse when a prefab-resolved child no longer sits where its prefab put it. The document
    has no way to say "this instance's child is elsewhere" short of editing the prefab, so a save
    that went ahead would report success and the next load would put the child back."""
    moved = []
    for obj in scene.collection.all_objects:
        if store.guid_of(obj) is None or not store.is_derived(obj):
            continue
        stored = next(
            (c.get("data") for c in store.component_json(obj)
             if isinstance(c, dict) and str(c.get("id", "")).lower() == well_known.TRANSFORM_ID),
            None,
        )
        if not _unchanged(stored if isinstance(stored, dict) else {}, _document_trs(obj)):
            moved.append(f"  '{obj.name}'")

    if not moved:
        return

    raise SaveError(
        "a prefab's child was moved, which this document cannot express:\n"
        + "\n".join(moved)
        + "\n\nMove the instance instead, or edit the prefab itself. Reload to put it back."
    )


def _merge(scene: bpy.types.Scene, base: PrefabDocument, result: SaveResult) -> PrefabDocument:
    """The document as Blender now has it, over the document as the file now has it. File order
    is kept: Blender guarantees no iteration order, and following it would reshuffle the file on
    every save. New objects follow, in name order."""
    objects = {store.guid_of(obj): obj for obj in _document_objects(scene)}
    # A group is a document object Blender happens to show as a collection (groups.py), so it
    # merges exactly like one -- same identity, same place in file order, same removal rule.
    collections = {groups.guid_of(found): found for found in _document_groups(scene)}
    present = frozenset(objects) | frozenset(collections)

    merged = PrefabDocument()
    for entry in base.objects:
        if entry.target is not None:
            # A carrier is the instance's data; it goes where the instance goes.
            if entry.parent in present:
                merged.objects.append(entry)
            else:
                result.removed += 1
            continue
        if (collection := collections.pop(entry.guid, None)) is not None:
            merged.objects.append(_group_entry(collection, entry, scene))
            continue
        obj = objects.pop(entry.guid, None)
        if obj is None:
            result.removed += 1
            continue
        merged.objects.append(_object_entry(obj, entry, result))

    for obj in sorted(objects.values(), key=lambda o: o.name):
        result.added += 1
        merged.objects.append(_object_entry(obj, None, result))

    for collection in sorted(collections.values(), key=lambda c: c.name):
        result.added += 1
        merged.objects.append(_group_entry(collection, None, scene))

    return merged


def _document_groups(scene: bpy.types.Scene) -> list:
    """Group collections under the scene, minting an identity for one the author just made.

    A new collection is given a ``uuid4`` rather than waited for: sidecars mint identities for
    FILES under ``assets/``, and an object inside a document has never been one of those --
    ``instancing.add_instance`` mints the same way for a placed instance.
    """
    found = []
    for collection in _walk_collections(scene.collection):
        if collection.name == LIBRARY_COLLECTION:
            continue
        if groups.guid_of(collection) is None:
            if not collection.objects and not collection.children:
                # Blender's own empty collection, or one the author has not put anything in:
                # a group with no children is not a group (groups.py).
                continue
            groups.tag(collection, str(uuid.uuid4()), collection.name)
        found.append(collection)
    return found


def _walk_collections(root: bpy.types.Collection):
    """Every collection under ``root``, excluding the mesh library and anything inside it."""
    for child in root.children:
        if child.name == LIBRARY_COLLECTION:
            continue
        yield child
        yield from _walk_collections(child)


def _holder_guid(collection: bpy.types.Collection, scene: bpy.types.Scene) -> str | None:
    """The document parent of a group: the group it sits inside, else the document ROOT.

    Not ``None`` at the top level, which is the whole point: a document has exactly one root, so
    a group linked straight into the scene collection hangs off the root exactly as an object
    dropped there does (``instancing._parent_to_document_root``). Returning nothing here wrote a
    second root and the save refused itself.
    """
    for candidate in _walk_collections(scene.collection):
        if collection.name in candidate.children:
            return groups.guid_of(candidate)
    return _root_guid(scene)


def _root_guid(scene: bpy.types.Scene) -> str | None:
    """The document root: the one object with no parent and no group around it. ``None`` when
    that is not unique -- the root rule then refuses the save and names the objects, which is a
    better error than anything this could invent."""
    found = [
        obj for obj in _document_objects(scene)
        if obj.parent is None
        and not any(groups.guid_of(c) is not None for c in obj.users_collection)
    ]
    return store.guid_of(found[0]) if len(found) == 1 else None


def _parent_guid(obj: bpy.types.Object, scene: bpy.types.Scene) -> tuple[str | None, str | None]:
    """The document parent of ``obj``, and a warning when Blender says two things at once.

    Membership wins WHERE THE GROUP HANGS WHERE THE OBJECT ALREADY HUNG, which is the ordinary
    authoring gesture and is transform-neutral by construction: a group's transform is the
    identity and its parent is the object's old parent, so re-hanging under it moves nothing.

    That case has to win, or the feature cannot be used at all. Dragging rows into a collection
    in the Outliner does NOT clear their object parenting, and every object in a real level is
    parented to the document root -- so a rule that let parenting win unconditionally discarded
    every group an author could actually make (#41).

    Parenting still wins when the group hangs somewhere ELSE, because then the two disagree about
    the transform space and only the parent's answer is the one the object is drawn at. Said out
    loud rather than discovered: the document has one parent link and cannot hold both.
    """
    parent = store.guid_of(obj.parent) if obj.parent is not None else None
    group = next(
        (c for c in obj.users_collection if groups.guid_of(c) is not None), None)
    if group is None:
        return parent, None

    if parent is None or _holder_guid(group, scene) == parent:
        return groups.guid_of(group), None

    return parent, (
        f"{obj.name} is parented to '{obj.parent.name}', which is not where its group "
        f"'{group.name}' hangs; the document keeps the parent, so its place in the group is "
        "not saved."
    )


def _group_entry(
    collection: bpy.types.Collection, original: PrefabObject | None, scene: bpy.types.Scene
) -> PrefabObject:
    """One group collection as a document object: meta, an identity transform, nothing else."""
    entry = PrefabObject() if original is None else original
    _write_meta(
        entry, groups.guid_of(collection), groups.name_of(collection),
        _holder_guid(collection, scene))
    if entry.component(well_known.TRANSFORM_ID) is None:
        # Spelled out rather than omitted, so the object reads as placed at the origin rather
        # than as one whose placement nobody wrote.
        entry.components.append(PrefabComponent(
            well_known.TRANSFORM_ID, well_known.TRANSFORM_TYPE, {
                well_known.POSITION: [0.0, 0.0, 0.0],
                well_known.ROTATION: [0.0, 0.0, 0.0, 1.0],
                well_known.SCALE: [1.0, 1.0, 1.0],
            }))
    return entry


def _object_entry(obj: bpy.types.Object, original: PrefabObject | None, result: SaveResult) -> PrefabObject:
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

    parent_guid, conflict = _parent_guid(obj, obj.users_scene[0] if obj.users_scene else bpy.context.scene)
    if conflict is not None:
        result.warnings.append(conflict)

    _write_meta(entry, guid, store.document_name(obj), parent_guid)
    _write_transform(entry, obj, original, result)

    # Last, so an overlay edit could never win against the meta/transform writes above.
    _apply_edits(obj, entry, result)
    return entry


def _apply_edits(obj: bpy.types.Object, entry: PrefabObject, result: SaveResult) -> None:
    """Apply pending add/remove, then field edits. A missing target is reported, not skipped:
    the document changed under the edit and the author's change is being dropped."""
    _apply_structure(obj, entry, result)
    pending = component_edits.read(obj)
    if not pending:
        return

    missing = [component_id for component_id in pending if entry.component(component_id) is None]
    result.edited += component_edits.apply_to(entry, pending)
    for component_id in missing:
        result.warnings.append(
            f"{obj.name}: an edit to component {component_id} was dropped -- the document no "
            "longer carries it.")


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


def _refresh_snapshots(scene: bpy.types.Scene, merged: PrefabDocument) -> None:
    """Rewrite each object's display JSON from the document that was just saved."""
    by_guid = {
        entry.guid.lower(): entry for entry in merged.objects if entry.guid
    }
    for obj in _document_objects(scene):
        guid = store.guid_of(obj)
        if not guid or guid.lower() not in by_guid:
            continue
        entry = by_guid[guid.lower()]
        store.tag_object(
            obj,
            guid,
            [
                {"id": component.id, "type": component.type, "data": component.data}
                for component in entry.components
            ],
        )
        store.tag_name(obj, entry.name)


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


def _document_objects(scene: bpy.types.Scene) -> list[bpy.types.Object]:
    """The objects this save may write. Derived (prefab-resolved) objects are excluded, or the
    merge would flatten every instance on the first save."""
    return [
        obj for obj in scene.collection.all_objects
        if store.guid_of(obj) is not None and not store.is_derived(obj)
    ]
