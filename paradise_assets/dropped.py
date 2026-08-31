"""Turning an object dropped from the Asset Browser into a real prefab instance.

Blender's Asset Browser drop is its OWN operation -- it appends or links the datablock and there
is no hook to replace that with ours, the way :class:`bpy.types.FileHandler` lets us own a file
drop. So the catalogue ships templates, Blender drops a copy of one, and this notices and converts
it.

**This is the fragile half of the feature and it is written defensively**, because a handler that
mutates objects behind the user's back is exactly the shape that produced the duplicate-identity
bug it now has to avoid:

* it acts only on an object carrying :data:`catalogue.TEMPLATE_KEY` and NO identity, so it can
  never touch a document object, and converting is idempotent -- the key is cleared first;
* it does nothing at all unless a document is open, so a dropped template in an unrelated file is
  left alone rather than silently rewritten;
* it mints a fresh identity per drop, which is the whole reason it exists: every copy of a
  template carries the catalogue's guid, and without this the second drop would collide with the
  first and refuse to save.

If this handler is ever the suspect, disabling it degrades to a visible template object rather
than to corruption -- the template has no identity, so the save path ignores it entirely.
"""

from __future__ import annotations

import json

import bpy

from . import catalogue
from .document import project
from .materialize import instancing, store

__all__ = ["register_handler", "unregister_handler"]


def _templates(scene) -> list:
    """Dropped templates awaiting conversion: the marker, and no identity yet."""
    return [
        obj for obj in scene.collection.all_objects
        if catalogue.TEMPLATE_KEY in obj and store.guid_of(obj) is None
    ]


def _convert(scene, obj) -> None:
    raw = obj.get(catalogue.TEMPLATE_KEY)

    # Cleared FIRST. If anything below raises, the object is left as an ordinary empty rather than
    # re-entering this on the next depsgraph update, which would be an unkillable error loop.
    del obj[catalogue.TEMPLATE_KEY]

    try:
        stored = json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        return

    guid, relative = stored.get("guid"), stored.get("path")
    if not guid or not relative:
        return

    state = store.read_state(scene)
    if state is None:
        return

    layout = project.locate(state.path)
    if layout is None:
        return

    instancing.adopt_template(scene, obj, guid, relative, layout)


@bpy.app.handlers.persistent
def _on_depsgraph_update(scene, depsgraph) -> None:
    # read_state is two dictionary lookups on the scene; doing it before scanning objects keeps
    # the common case -- a file that is not a Paradise document -- at almost no cost per update.
    if store.read_state(scene) is None:
        return

    pending = _templates(scene)
    if not pending:
        return

    for obj in pending:
        _convert(scene, obj)


def register_handler() -> None:
    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)


def unregister_handler() -> None:
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
