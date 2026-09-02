"""Converting an Asset Browser drop into a prefab instance. The drop is Blender's own operation
with no hook to replace it, so a ``depsgraph_update_post`` handler follows it. Written
defensively: it acts only on a template with NO identity, clears the key first, and mints a
fresh identity per drop (every template copy carries the catalogue's guid, so the second drop
would otherwise collide). Disabling it degrades to a visible template, never corruption.
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

    # Cleared FIRST, or a raise below re-enters on the next depsgraph update forever.
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
    # Cheap check first: this runs on every depsgraph update in every file.
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
