"""Blender objects -> scene document.

Blender owns placement: names, parents, transforms, and which objects exist. The document owns
component payloads, which this addon does not edit and therefore takes from the file rather than
from Blender -- so a component it has never heard of round-trips verbatim.

Three rules guard the write, each against a failure that has a name:

* **Re-read and merge.** The document is re-read at save time and used as the base. A hand edit
  made since the load survives, because only what Blender owns is overwritten.
* **Stamp.** If the file changed since it was read, the save REFUSES. Merging blind would
  silently drop whatever made the change.
* **Atomic.** Written to a temp file beside the target and replaced, so an interrupted save
  cannot leave a half-written document as the source of truth.

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

from ..document import axes, scene as scene_document
from ..document.scene import SceneDocument, SceneObject, SceneTransform
from . import store

__all__ = ["SaveError", "SaveResult", "save_scene"]

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


def save_scene(scene: bpy.types.Scene) -> SaveResult:
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
        base = scene_document.loads(handle.read(), state.path)

    result = SaveResult()
    merged = _merge(scene, base, result)

    _write_atomic(state.path, scene_document.dumps(merged))
    store.write_state(scene, state.path)
    result.written = len(merged.objects)
    return result


def _merge(scene: bpy.types.Scene, base: SceneDocument, result: SaveResult) -> SceneDocument:
    """The document as Blender now has it, over the document as the file now has it."""
    by_guid = base.by_guid()
    objects = _document_objects(scene)
    present = {store.guid_of(obj) for obj in objects}

    merged = SceneDocument()
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


def _document_order(base: SceneDocument):
    """Sort key keeping the file's existing order, with new objects appended in Blender order."""
    order = {entry.guid: index for index, entry in enumerate(base.objects)}
    return lambda entry: (order.get(entry.guid, len(order)), entry.name)


def _object_entry(obj: bpy.types.Object, original: SceneObject | None, result: SaveResult) -> SceneObject:
    guid = store.guid_of(obj)
    entry = SceneObject(guid=guid, name=obj.name)

    parent_guid = store.guid_of(obj.parent) if obj.parent is not None else None
    if obj.parent is not None and parent_guid is None:
        # Parented to something the document does not contain. Recording it is impossible and
        # dropping it silently would move the object, so say so and treat it as a root.
        result.warnings.append(
            f"{obj.name} is parented to '{obj.parent.name}', which is not a document object; "
            "saved as a root object"
        )
    entry.parent = parent_guid

    # Components are the FILE's, never Blender's. This is the line that makes an unrecognised
    # component safe to open.
    if original is not None:
        entry.components = original.components

    entry.transform = _transform_of(obj, original, result)
    return entry


def _transform_of(obj: bpy.types.Object, original: SceneObject | None, result: SaveResult) -> SceneTransform:
    position, rotation, scale = _blender_trs(obj)
    document = axes.from_blender_trs(position, rotation, scale)

    if original is not None and _unchanged(original.transform, document):
        return original.transform

    if original is not None:
        result.moved += 1
    return SceneTransform(position=document[0], rotation=document[1], scale=document[2])


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


def _unchanged(stored: SceneTransform, computed) -> bool:
    """Whether the object still sits where the document put it.

    Compared per-component and RELATIVE to the stored magnitude, because the error being tolerated
    is a relative one: a position of 400 metres and a scale of 0.01 do not deserve the same
    absolute slack. Rotations compare by the dot product, since a quaternion and its negation are
    the same rotation and a sign flip is not a move.
    """
    for a, b in ((stored.position, computed[0]), (stored.scale, computed[2])):
        for x, y in zip(a, b):
            if abs(x - y) > _EPSILON * max(abs(x), 1.0):
                return False

    dot = abs(sum(x * y for x, y in zip(stored.rotation, computed[1])))
    return abs(1.0 - dot) <= _EPSILON


def _document_objects(scene: bpy.types.Scene) -> list[bpy.types.Object]:
    return [obj for obj in scene.collection.all_objects if store.guid_of(obj) is not None]


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
