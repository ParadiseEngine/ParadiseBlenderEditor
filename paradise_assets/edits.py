"""Component field edits as an OVERLAY (``{component id: {path: value}}``) over the document.

Only touched members are stored and the file's payload is the base at save, so an unedited
component is written byte-for-byte and an unedited field keeps a value this addon may have no
schema for. Materializing every payload into Blender properties was rejected because ID
properties normalize types (``int`` -> ``float``, tuple -> list), which is a bug in every
component rather than the edited one. Add/remove live beside the field map and apply first.
The overlay is cleared once applied, or an old edit could resurrect itself over a newer value.
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

# No ``bpy`` import (the hints are never-evaluated strings): this is the only new logic on the
# save path, and importability outside Blender is what lets it be tested against a plain dict.

EDITS_KEY = "paradise_edits"

#: Pending add/remove, separate from :data:`EDITS_KEY` so :func:`read` never mistakes a
#: payload for a field map.
STRUCTURE_KEY = "paradise_structure"


def read(obj: bpy.types.Object) -> dict[str, dict[str, object]]:
    """This object's pending edits, ``{component id: {field: value}}``."""
    raw = obj.get(EDITS_KEY)
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Losing pending edits beats a save that cannot proceed at all.
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
    """Record *value* at a slash path, dropping overlay keys it contains or is contained by:
    otherwise a whole-array replace and a cell edit would both apply and ``sort_keys`` would
    decide which won."""
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
    """Forget one field's edit, one component's, or the object's (which is the whole of revert)."""
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
    """Queue *spec* as a new component, or undo a pending remove of the same id (restoring the
    file's payload, since the remove never reached the document)."""
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
    """Queue *component_id* for deletion, or drop it from a pending add, with its field edits."""
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
    """What the panel draws: the load-time snapshot minus removals plus adds. The snapshot itself
    stays the file's version, so revert is dropping the structure key."""
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
    """Apply *edits* in place; returns fields written. A component the document no longer
    carries is skipped, never created from a partial payload missing every other field."""
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
    """Write a slash path, creating containers by the segment AFTER each one; holes grow as empty
    rows rather than compacting, since reindexing would hide the bug that produced them."""
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
