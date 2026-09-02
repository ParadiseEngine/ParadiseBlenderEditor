"""Component field edits, held as an OVERLAY on the document rather than as a copy of it.

The save path re-reads the document and overwrites only what Blender owns -- the name, the
parent, the transform. Everything else is carried through untouched, and that is exactly what
lets a scene full of components this addon has never heard of survive a round trip
(:mod:`.materialize.save`). Editing a component field has to join that list without ending it.

**So an edit is recorded as a field-level change, not as a payload.** What is stored is
``{component id: {path: value}}`` -- only the members an author actually touched, addressed by
slash path so a nested leaf (``Camera/Guide/NearDistance``) or a list row (``Slots/0``) is the
same kind of edit as a top-level float. At save the base payload comes from the FILE and the
overlay is applied on top, so:

  - a component nobody edited is written byte-for-byte as it was read;
  - a field nobody edited keeps whatever the file says, including a value this addon has no
    schema for and could not have displayed;
  - an edit survives the document changing underneath it, as long as the component is still
    there -- which is what makes "reload, then save" not silently discard your work.

The alternative -- materializing every payload into Blender properties and writing them all back
-- was rejected for the reason :mod:`.materialize.store` gives for storing payloads as a JSON
string: Blender's ID property system normalizes types on the way in and out, so an ``int`` comes
back a ``float`` and a tuple a list. For data we promise to return verbatim, "nearly the same
value" is a bug, and it would be a bug in every component in the scene rather than in the one
being edited.

**Whole components join the same overlay.** Adding or removing a component is not a field path,
and inventing a payload out of edited members is exactly what :func:`apply_to` refuses to do.
Those changes live beside the field map as ``{added: [...], removed: [...]}`` and are applied
first at save, so a field edit on a component that was just added still has somewhere to land.
``meta`` and ``transform`` are never recorded here -- Blender owns those.

**The overlay is cleared when it is applied.** An edit is a pending change to the document, not a
second place the value lives; once the save has written it, the file is the truth again. Keeping
it would mean an old edit could resurrect itself over a newer value someone else wrote.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import bpy

__all__ = [
    "EDITS_KEY",
    "STRUCTURE_KEY",
    "add_component",
    "added_components",
    "apply_to",
    "clear",
    "count",
    "edited_fields",
    "read",
    "read_path",
    "read_structure",
    "remove_component",
    "removed_ids",
    "set_field",
    "visible_components",
    "write_path",
]

# THIS MODULE IMPORTS NO ``bpy``, and the ``bpy.types.Object`` hints below are strings rather
# than types (``from __future__ import annotations``, never evaluated). That is deliberate and
# worth keeping: the overlay is the only new logic on the save path, and being importable outside
# Blender is what lets it be tested against a plain dict -- which supports the whole of the
# interface it needs -- instead of only through an integration run. Same rule and same reason as
# ``document/``, one directory up.

#: The pending field edits, as a JSON string. A string for the same reason payloads are one.
EDITS_KEY = "paradise_edits"

#: Pending add/remove of whole components, as a JSON string.
#:
#: Separate from :data:`EDITS_KEY` because every value there is a field map, and a newly added
#: component is a payload -- mixing the two would make :func:`read` drop the structural half.
STRUCTURE_KEY = "paradise_structure"


def read(obj: bpy.types.Object) -> dict[str, dict[str, object]]:
    """This object's pending edits, ``{component id: {field: value}}``."""
    raw = obj.get(EDITS_KEY)
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Hand-edited or written by an older addon. Dropping it loses pending edits, which is
        # the lesser harm: the alternative is a save that cannot proceed at all.
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(component): dict(fields)
        for component, fields in parsed.items()
        if isinstance(fields, dict)
    }


def _write(obj: bpy.types.Object, edits: dict[str, dict[str, object]]) -> None:
    if edits:
        obj[EDITS_KEY] = json.dumps(edits, sort_keys=True)
    elif EDITS_KEY in obj:
        del obj[EDITS_KEY]


def set_field(obj: bpy.types.Object, component_id: str, field: str, value) -> None:
    """Record *value* for one field of one component.

    *field* is a slash path (``MaxSpeed``, ``Camera/Guide/NearDistance``, ``Slots/0``). Writing
    a path drops overlay keys that are it, its descendants, or an ancestor that would contain
    it -- otherwise a whole-array replace and a cell edit would both apply, and ``sort_keys``
    would decide which won.
    """
    edits = read(obj)
    fields = edits.setdefault(component_id, {})
    stale = [
        key for key in fields
        if key == field or key.startswith(field + "/") or field.startswith(key + "/")
    ]
    for key in stale:
        del fields[key]
    fields[field] = value
    if not fields:
        del edits[component_id]
    _write(obj, edits)


def clear(obj: bpy.types.Object, component_id: str | None = None, field: str | None = None) -> None:
    """Forget one field's edit, one component's, or the object's.

    Forgetting is REVERTING: the file's value is what a field with no edit shows, so dropping the
    entry is the whole of "undo this change", with nothing to restore from. Clearing the object
    also drops pending add/remove, which are the same kind of pending change.
    """
    if component_id is None:
        if EDITS_KEY in obj:
            del obj[EDITS_KEY]
        if STRUCTURE_KEY in obj:
            del obj[STRUCTURE_KEY]
        return

    edits = read(obj)
    if component_id not in edits:
        return
    if field is None:
        del edits[component_id]
    else:
        edits[component_id].pop(field, None)
        if not edits[component_id]:
            del edits[component_id]
    _write(obj, edits)


def edited_fields(obj: bpy.types.Object, component_id: str) -> dict[str, object]:
    """One component's pending edits."""
    return read(obj).get(component_id, {})


def count(obj: bpy.types.Object) -> int:
    """How many pending changes the panel should report: fields plus add/remove."""
    return (
        sum(len(fields) for fields in read(obj).values())
        + len(added_components(obj))
        + len(removed_ids(obj))
    )


def read_structure(obj: bpy.types.Object) -> dict[str, list]:
    """Pending add/remove, ``{"added": [...], "removed": [...]}``."""
    raw = obj.get(STRUCTURE_KEY)
    if not isinstance(raw, str) or not raw:
        return {"added": [], "removed": []}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"added": [], "removed": []}
    if not isinstance(parsed, dict):
        return {"added": [], "removed": []}
    added = [
        spec for spec in parsed.get("added") or []
        if isinstance(spec, dict) and spec.get("id")
    ]
    removed = [str(item) for item in parsed.get("removed") or [] if item]
    return {"added": added, "removed": removed}


def _write_structure(obj: bpy.types.Object, structure: dict[str, list]) -> None:
    added = structure.get("added") or []
    removed = structure.get("removed") or []
    if added or removed:
        obj[STRUCTURE_KEY] = json.dumps({"added": added, "removed": removed}, sort_keys=True)
    elif STRUCTURE_KEY in obj:
        del obj[STRUCTURE_KEY]


def added_components(obj: bpy.types.Object) -> list[dict]:
    """Components queued to be inserted at save, each ``{id, type, data}``."""
    return read_structure(obj)["added"]


def removed_ids(obj: bpy.types.Object) -> list[str]:
    """Component ids queued to be dropped at save."""
    return read_structure(obj)["removed"]


def add_component(obj: bpy.types.Object, spec: dict) -> None:
    """Queue *spec* as a new component, or undo a pending remove of the same id.

    Re-adding a component that was only removed this session restores the FILE's payload rather
    than inserting an empty one -- the remove never reached the document, so there is nothing
    to replace.
    """
    component_id = str(spec.get("id", ""))
    if not component_id:
        return
    structure = read_structure(obj)
    needle = component_id.lower()
    kept_removed = [item for item in structure["removed"] if item.lower() != needle]
    if len(kept_removed) != len(structure["removed"]):
        structure["removed"] = kept_removed
        _write_structure(obj, structure)
        return
    if any(str(item.get("id", "")).lower() == needle for item in structure["added"]):
        return
    data = spec.get("data")
    structure["added"].append({
        "id": component_id,
        "type": spec.get("type"),
        "data": dict(data) if isinstance(data, dict) else {},
    })
    _write_structure(obj, structure)


def remove_component(obj: bpy.types.Object, component_id: str) -> None:
    """Queue *component_id* for deletion, or drop it from a pending add.

    Field edits for that id go with it: they addressed a component that will not be there.
    """
    if not component_id:
        return
    structure = read_structure(obj)
    needle = component_id.lower()
    kept_added = [
        spec for spec in structure["added"] if str(spec.get("id", "")).lower() != needle
    ]
    if len(kept_added) != len(structure["added"]):
        structure["added"] = kept_added
        _write_structure(obj, structure)
        clear(obj, component_id)
        return
    if not any(item.lower() == needle for item in structure["removed"]):
        structure["removed"].append(component_id)
    _write_structure(obj, structure)
    clear(obj, component_id)


def visible_components(snapshot: list, structure: dict | None = None) -> list:
    """What the panel should draw: the load-time snapshot, minus removals, plus adds.

    The snapshot stays the file's version. Display merges, the same way field widgets merge the
    overlay -- so reverting add/remove is dropping the structure key, not reconstructing a copy.
    """
    added = (structure or {}).get("added") or []
    removed = {str(item).lower() for item in (structure or {}).get("removed") or []}
    visible: list = []
    present: set[str] = set()
    for component in snapshot:
        if not isinstance(component, dict):
            continue
        component_id = str(component.get("id", "")).lower()
        if not component_id or component_id in removed:
            continue
        visible.append(component)
        present.add(component_id)
    for spec in added:
        if not isinstance(spec, dict):
            continue
        component_id = str(spec.get("id", "")).lower()
        if component_id and component_id not in present:
            visible.append(spec)
            present.add(component_id)
    return visible


def apply_to(entry, edits: dict[str, dict[str, object]]) -> int:
    """Apply *edits* to a document object in place; return how many fields were written.

    A component the overlay names but the document no longer carries is SKIPPED rather than
    created. An edit addresses a component that was there when it was made, so its absence means
    the document moved on -- and inventing a component out of a partial payload would produce one
    with only the edited members, missing every other field the game expects.
    """
    written = 0
    for component_id, fields in edits.items():
        component = entry.component(component_id)
        if component is None:
            continue
        for path, value in fields.items():
            write_path(component.data, path, value)
            written += 1
    return written


def read_path(data: dict, path: str):
    """The value at a slash path, or ``None`` when a segment is missing."""
    node: object = data
    for part in path.split("/"):
        if isinstance(node, list):
            if not part.isdigit():
                return None
            index = int(part)
            if index < 0 or index >= len(node):
                return None
            node = node[index]
            continue
        if not isinstance(node, dict):
            return None
        if part not in node:
            return None
        node = node[part]
    return node


def write_path(root: dict, path: str, value) -> None:
    """Write a slash path into a nested payload, creating objects and lists as needed.

    Which container a segment creates is decided by the segment AFTER it: ``Tables/0/Table``
    means a list at ``Tables`` and an object at index 0. Indices grow with empty rows rather
    than compacting a hole -- silently reindexing would hide the bug that produced it.
    """
    parts = path.split("/")
    target: object = root
    for depth, part in enumerate(parts[:-1]):
        target = _child(target, part, wants_list=parts[depth + 1].isdigit())
    _assign(target, parts[-1], value)


def _child(container, part: str, wants_list: bool):
    kind = list if wants_list else dict
    if isinstance(container, list):
        index = int(part)
        _grow(container, index, kind)
        if not isinstance(container[index], kind):
            container[index] = kind()
        return container[index]
    nested = container.get(part) if isinstance(container, dict) else None
    if not isinstance(nested, kind):
        nested = kind()
        if isinstance(container, dict):
            container[part] = nested
    return nested


def _assign(container, part: str, value) -> None:
    if isinstance(container, list):
        index = int(part)
        _grow(container, index, lambda: None)
        container[index] = value
        return
    if isinstance(container, dict):
        container[part] = value


def _grow(target: list, index: int, kind) -> None:
    """Extend a list so ``index`` exists. *kind* is a factory -- one shared ``{}`` appended
    twice would make two rows the same object."""
    factory = kind if callable(kind) else (lambda: kind)
    while len(target) <= index:
        target.append(factory())
