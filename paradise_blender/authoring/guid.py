"""Stored per-placement entity GUIDs. Shift+D/Alt+D/copy-paste deep-copy property groups, so a
duplicate arrives already colliding and there is no callback to hook; hence mint lazily and
sweep on ``save_pre`` (the exporter does NOT sweep). Collisions keep the first object in a
stable name order, or which duplicate keeps its identity would vary between runs. See #27 for
the split between this stored GUID and the exported ``meta.Guid``.
"""

from __future__ import annotations

import uuid

import bpy

from .. import log
from .entity import entity_objects

__all__ = ["EMPTY_GUID", "ensure_entity_guid", "ensure_unique_guids", "parse_guid", "register", "unregister"]

EMPTY_GUID = uuid.UUID(int=0)


def parse_guid(text: str) -> uuid.UUID:
    """Parse a stored GUID (hyphenated, or the Godot host's undashed form); ``EMPTY_GUID`` if malformed."""
    if not text:
        return EMPTY_GUID
    try:
        return uuid.UUID(text)
    except ValueError:
        return EMPTY_GUID


def ensure_entity_guid(obj: bpy.types.Object) -> uuid.UUID:
    """The object's GUID, minted and persisted if absent (an all-zero GUID would collide)."""
    current = parse_guid(obj.paradise.entity_guid)
    if current != EMPTY_GUID:
        return current

    minted = uuid.uuid4()
    obj.paradise.entity_guid = str(minted)
    return minted


def ensure_unique_guids(scene: bpy.types.Scene) -> int:
    """Mint missing GUIDs and re-mint duplicates in stable name order; returns changes."""
    seen: dict[uuid.UUID, str] = {}
    changed = 0

    for obj in entity_objects(scene):
        current = parse_guid(obj.paradise.entity_guid)

        if current == EMPTY_GUID:
            obj.paradise.entity_guid = str(uuid.uuid4())
            changed += 1
            current = parse_guid(obj.paradise.entity_guid)
        elif current in seen:
            # Say so: the author may have expected the copy to BE the same entity.
            log.warn(
                f"'{obj.name}' shared an entity GUID with '{seen[current]}' (usually the result "
                f"of duplicating an object). A new GUID was minted for '{obj.name}'."
            )
            obj.paradise.entity_guid = str(uuid.uuid4())
            changed += 1
            current = parse_guid(obj.paradise.entity_guid)

        seen[current] = obj.name

    return changed


@bpy.app.handlers.persistent
def _on_save_pre(_file_path) -> None:  # Blender passes the path, unused
    """Sweep before the .blend is written. ``@persistent`` or Blender drops the handler on the
    first file load and identities silently stop being maintained."""
    for scene in bpy.data.scenes:
        changed = ensure_unique_guids(scene)
        if changed:
            log.info(f"Minted or repaired {changed} entity GUID(s) in scene '{scene.name}'.")


def register() -> None:
    if _on_save_pre not in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.append(_on_save_pre)


def unregister() -> None:
    if _on_save_pre in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.remove(_on_save_pre)
