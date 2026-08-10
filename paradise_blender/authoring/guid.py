"""Stable per-placement entity identity.

``LevelEntityData.EntityGuid`` is how the runtime correlates an entity across reloads, across
a live-preview patch, and across a scene rebuild. It must be stable for a given placement and
unique within a scene.

Blender makes the uniqueness half harder than Godot does. Duplicating an object (``Shift+D``,
``Alt+D``, or a copy/paste) deep-copies its property groups, GUID included -- so a duplicate
arrives *already colliding* with its source, silently. There is no per-object "was duplicated"
callback to hook. So the lifecycle mirrors the Godot host's: mint lazily, and sweep for
collisions on save (``bpy.app.handlers.save_pre``, the analogue of
``NOTIFICATION_EDITOR_PRE_SAVE``).

The sweep is also run by the exporter before it walks the scene, so an unsaved .blend still
exports valid, unique identities rather than a scene full of duplicated GUIDs.

Collision resolution keeps the *first* object in a stable ordering and re-mints the others.
Without a deterministic order, which of two duplicates keeps the original GUID would vary
between runs, and an entity's identity would flicker across exports.
"""

from __future__ import annotations

import uuid

import bpy

from .. import log
from .entity import entity_objects

__all__ = ["EMPTY_GUID", "ensure_entity_guid", "ensure_unique_guids", "parse_guid", "register", "unregister"]

EMPTY_GUID = uuid.UUID(int=0)


def parse_guid(text: str) -> uuid.UUID:
    """Parse a stored GUID string, returning :data:`EMPTY_GUID` when absent or malformed.

    Accepts both the hyphenated form this addon writes and the 32-hex-digit "N" form the
    Godot host stores in node metadata, so a scene migrated between tools keeps its identities.
    """
    if not text:
        return EMPTY_GUID
    try:
        return uuid.UUID(text)
    except ValueError:
        return EMPTY_GUID


def ensure_entity_guid(obj: bpy.types.Object) -> uuid.UUID:
    """Return the object's GUID, minting and persisting one if it has none.

    The exporter calls this so a freshly-created, never-saved entity exports a real identity
    instead of the all-zero GUID -- which would collide across every such entity at runtime.
    """
    current = parse_guid(obj.paradise.entity_guid)
    if current != EMPTY_GUID:
        return current

    minted = uuid.uuid4()
    obj.paradise.entity_guid = str(minted)
    return minted


def ensure_unique_guids(scene: bpy.types.Scene) -> int:
    """Mint missing GUIDs and re-mint duplicates. Returns the number of GUIDs changed.

    Iterates in the stable name order :func:`..authoring.entity.entity_objects` provides, so
    the object that keeps a contested GUID is the same one on every run.
    """
    seen: dict[uuid.UUID, str] = {}
    changed = 0

    for obj in entity_objects(scene):
        current = parse_guid(obj.paradise.entity_guid)

        if current == EMPTY_GUID:
            obj.paradise.entity_guid = str(uuid.uuid4())
            changed += 1
            current = parse_guid(obj.paradise.entity_guid)
        elif current in seen:
            # Almost always a duplicated object. Re-mint the later one and say so: the author
            # may have expected the copy to *be* the same entity.
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
    """Sweep every scene before the .blend is written.

    ``@persistent`` is required or the handler is dropped the first time a file is loaded --
    Blender clears non-persistent handlers on load, and the failure mode (identities silently
    stop being maintained after the first file open) is invisible until something collides.
    """
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
