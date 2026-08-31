"""Blender objects -> scene document.

Blender owns placement: names, parents, transforms, and which objects exist. The document owns
component payloads, which this addon does not edit and therefore takes from the file rather than
from Blender -- so a component it has never heard of round-trips verbatim.

Four rules guard the write, each against a failure that has a name:

* **Re-read and merge.** The document is re-read at save time and used as the base. A hand edit
  made since the load survives, because only what Blender owns is overwritten.
* **Stamp.** If the file changed since it was read, the save REFUSES. Merging blind would
  silently drop whatever made the change.
* **Atomic.** Written to a temp file beside the target and replaced, so an interrupted save
  cannot leave a half-written document as the source of truth.
* **The addon is the guarantor.** Every saved entity carries a ``meta`` AND a ``transform``
  component, and a parent must be an entity in the same document -- refused, not warned, because
  since contract v6 nothing downstream re-checks or synthesizes placement: what the addon writes
  is what the game loads.

And one rule that is not about safety but about diffs:

* **An object nobody moved keeps its authored numbers verbatim.** The document stores values
  that came from C# ``float``; Blender stores float32 too, and the rebase runs a square root.
  Round-tripping is therefore accurate to ~4e-8 relative -- fine as a position, fatal as text,
  because ``repr`` of a value that moved in the last bit is a completely different string. Left
  alone, saving an untouched scene would rewrite every transform in it and bury a one-object
  edit in a whole-file diff. See :func:`_unchanged`.
"""

from __future__ import annotations

import os
import tempfile

import bpy

from ..document import axes, prefab as prefab_document, well_known
from ..document.asset_reference import AssetReference
from ..document.prefab import PrefabComponent, PrefabDocument, PrefabObject
from . import store

__all__ = ["SaveError", "SaveResult", "save_prefab"]

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

    result = SaveResult()
    merged = _merge(scene, base, result)

    _write_atomic(state.path, prefab_document.dumps(merged))
    store.write_state(scene, state.path)
    result.written = len(merged.objects)
    return result


def _refuse_duplicate_identities(scene: bpy.types.Scene) -> None:
    """Refuse to save when two objects claim one identity.

    **Duplicating a document object is the obvious thing to try, and it silently destroyed the
    file.** Blender copies an object's custom properties, identity included, so Shift+D produces a
    second object claiming the first one's guid. The merge then wrote both, the document declared
    the same guid twice, and it would not load again -- while the save reported success.

    Refusing here rather than repairing it: this addon does not know which of the two the author
    meant to keep, and minting a fresh guid for one of them would quietly turn a copy of an
    instance into a second instance the document never had. Naming the objects lets the author
    decide in a second, which is the part that was missing.
    """
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
    """Refuse to save when an entity is parented to something that is not an entity.

    A parent link is recorded as ``meta.Parent`` naming another document object; a Blender-only
    parent has no identity to name. The old behaviour warned and saved the object AS A ROOT --
    which silently moved it, because Blender still showed the hierarchy the document no longer
    had. Since contract v6 the parent chain ships to the runtime, so a link the document cannot
    express is a link the game never sees: refusing names the objects and lets the author decide
    in a second.
    """
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


def _merge(scene: bpy.types.Scene, base: PrefabDocument, result: SaveResult) -> PrefabDocument:
    """The document as Blender now has it, over the document as the file now has it."""
    by_guid = base.by_guid()
    objects = _document_objects(scene)
    present = {store.guid_of(obj) for obj in objects}

    merged = PrefabDocument()
    seen: set[str] = set()

    # Document order is preserved for objects that were already there -- Blender guarantees no
    # iteration order for a scene's objects, so following it instead would reshuffle the file on
    # every save and make each diff unreadable.
    for entry in base.objects:
        if entry.guid not in present:
            result.removed += 1
            continue
        seen.add(entry.guid)

    for obj in objects:
        guid = store.guid_of(obj)
        original = by_guid.get(guid)
        if original is None:
            result.added += 1
        merged.objects.append(_object_entry(obj, original, result))

    merged.objects.sort(key=_document_order(base))
    return merged


def _document_order(base: PrefabDocument):
    """Sort key keeping the file's existing order, with new objects appended in Blender order."""
    order = {entry.guid: index for index, entry in enumerate(base.objects)}
    return lambda entry: (order.get(entry.guid, len(order)), entry.name or "")


def _object_entry(obj: bpy.types.Object, original: PrefabObject | None, result: SaveResult) -> PrefabObject:
    """One Blender object as a document object.

    The FILE's version is the base and Blender overwrites only what Blender owns -- the name, the
    parent, and the transform's three fields. Everything else, a prefab reference and every
    component payload included, is carried through untouched. That is what lets a scene full of
    components this addon has never heard of survive a round trip, and what keeps an instance an
    instance instead of flattening it into the plain objects it displays as.
    """
    guid = store.guid_of(obj)
    entry = PrefabObject() if original is None else original

    # A NEW instance has no file entry to carry a prefab reference through, so it carries its own.
    # Only when there is no original: an object already in the document keeps whatever reference
    # the file gives it, which is what stops a stale marker from overriding an edited document.
    if original is None and store.prefab_of(obj) is not None:
        reference_guid, reference_path = store.prefab_of(obj)
        entry.prefab = AssetReference(reference_guid, reference_path)

    # A foreign parent was refused before the merge began (_refuse_foreign_parents), so a parent
    # here always has an identity to record.
    parent_guid = store.guid_of(obj.parent) if obj.parent is not None else None

    _write_meta(entry, guid, obj.name, parent_guid)
    _write_transform(entry, obj, original, result)
    return entry


def _write_meta(entry: PrefabObject, guid: str, name: str, parent: str | None) -> None:
    """Update identity, name and parent in place, leaving any other meta field alone."""
    component = entry.component(well_known.META_ID)
    if component is None:
        component = PrefabComponent(well_known.META_ID, well_known.META_TYPE, {})
        entry.components.insert(0, component)

    component.data[well_known.GUID] = guid
    component.data[well_known.NAME] = name
    if parent is None:
        component.data.pop(well_known.PARENT, None)
    else:
        component.data[well_known.PARENT] = parent


def _write_transform(
    entry: PrefabObject, obj: bpy.types.Object, original: PrefabObject | None, result: SaveResult
) -> None:
    position, rotation, scale = axes.from_blender_trs(*_blender_trs(obj))

    stored = original.component(well_known.TRANSFORM_ID) if original is not None else None
    if stored is not None:
        if _unchanged(stored.data, (position, rotation, scale)):
            return   # untouched: keep the authored numbers verbatim
        result.moved += 1
    elif original is not None and not _unchanged({}, (position, rotation, scale)):
        result.moved += 1

    # An object the document gave no transform GETS one now, even at the identity: every saved
    # entity carries a transform, because since contract v6 nothing downstream synthesizes
    # placement -- an absent component would be whatever each loader decides it means. The
    # one-time diff on a previously transform-less object is the rule taking effect.

    component = entry.component(well_known.TRANSFORM_ID)
    if component is None:
        component = PrefabComponent(well_known.TRANSFORM_ID, well_known.TRANSFORM_TYPE, {})
        entry.components.append(component)

    component.data[well_known.POSITION] = [float(v) for v in position]
    component.data[well_known.ROTATION] = [float(v) for v in rotation]
    component.data[well_known.SCALE] = [float(v) for v in scale]


def _blender_trs(obj: bpy.types.Object):
    """The object's LOCAL transform, as ``(position, (x, y, z, w), scale)``.

    Read from the channels rather than from ``matrix_basis`` for the same reason the loader
    writes to them: a matrix round trip decomposes, and decomposition is lossy.
    """
    if obj.rotation_mode == "QUATERNION":
        w, x, y, z = obj.rotation_quaternion
    else:
        w, x, y, z = obj.rotation_euler.to_quaternion()
    return (
        tuple(float(v) for v in obj.location),
        (float(x), float(y), float(z), float(w)),
        tuple(float(v) for v in obj.scale),
    )


def _unchanged(stored: dict, computed) -> bool:
    """Whether the object still sits where the document put it.

    Compared per-component and RELATIVE to the stored magnitude, because the error being tolerated
    is a relative one: a position of 400 metres and a scale of 0.01 do not deserve the same
    absolute slack. Rotations compare by the dot product, since a quaternion and its negation are
    the same rotation and a sign flip is not a move.
    """
    # Sequence, not `list`: an ABSENT field falls back to the identity default below, and those
    # are tuples. Testing for `list` alone made every identity transform compare as changed, so
    # every object the document gave no transform gained one on the first save.
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
        for x, y in zip(authored, computed[index]):
            if abs(x - y) > _EPSILON * max(abs(x), 1.0):
                return False

    rotation = numbers(well_known.ROTATION, 4, (0.0, 0.0, 0.0, 1.0))
    if rotation is None:
        return False
    dot = abs(sum(x * y for x, y in zip(rotation, computed[1])))
    return abs(1.0 - dot) <= _EPSILON


def _document_objects(scene: bpy.types.Scene) -> list[bpy.types.Object]:
    """The objects this save may write.

    Objects RESOLVED out of a prefab are excluded: the document has no entry for them, so the
    merge would count them "added" and write them into the scene as plain objects -- flattening
    every instance on the first save, and duplicating them on the next load.
    """
    return [
        obj for obj in scene.collection.all_objects
        if store.guid_of(obj) is not None and not store.is_derived(obj)
    ]


def _write_atomic(path: str, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file in the same directory, then replace.

    Same directory because ``os.replace`` is only atomic within one filesystem; a temp file in
    the system temp directory can land on another volume and degrade to a copy.
    """
    directory = os.path.dirname(path)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=directory, delete=False, suffix=".tmp"
    )
    try:
        with handle:
            handle.write(text)
        os.replace(handle.name, path)
    except BaseException:
        with_suppress = getattr(os, "unlink", None)
        if with_suppress is not None and os.path.exists(handle.name):
            os.unlink(handle.name)
        raise
