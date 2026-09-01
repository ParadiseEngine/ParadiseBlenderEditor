"""Component field edits, held as an OVERLAY on the document rather than as a copy of it.

The save path re-reads the document and overwrites only what Blender owns -- the name, the
parent, the transform. Everything else is carried through untouched, and that is exactly what
lets a scene full of components this addon has never heard of survive a round trip
(:mod:`.materialize.save`). Editing a component field has to join that list without ending it.

**So an edit is recorded as a field-level change, not as a payload.** What is stored is
``{component id: {field: value}}`` -- only the members an author actually touched. At save the
base payload comes from the FILE and the overlay is applied on top, so:

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
    "apply_to",
    "clear",
    "count",
    "edited_fields",
    "read",
    "set_field",
]

# THIS MODULE IMPORTS NO ``bpy``, and the ``bpy.types.Object`` hints below are strings rather
# than types (``from __future__ import annotations``, never evaluated). That is deliberate and
# worth keeping: the overlay is the only new logic on the save path, and being importable outside
# Blender is what lets it be tested against a plain dict -- which supports the whole of the
# interface it needs -- instead of only through an integration run. Same rule and same reason as
# ``document/``, one directory up.

#: The pending edits, as a JSON string. A string for the same reason payloads are one.
EDITS_KEY = "paradise_edits"


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
    """Record *value* for one field of one component."""
    edits = read(obj)
    edits.setdefault(component_id, {})[field] = value
    _write(obj, edits)


def clear(obj: bpy.types.Object, component_id: str | None = None, field: str | None = None) -> None:
    """Forget one field's edit, one component's, or the object's.

    Forgetting is REVERTING: the file's value is what a field with no edit shows, so dropping the
    entry is the whole of "undo this change", with nothing to restore from.
    """
    if component_id is None:
        if EDITS_KEY in obj:
            del obj[EDITS_KEY]
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
    """How many fields are pending across every component, for a panel to report."""
    return sum(len(fields) for fields in read(obj).values())


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
        for name, value in fields.items():
            component.data[name] = value
            written += 1
    return written
