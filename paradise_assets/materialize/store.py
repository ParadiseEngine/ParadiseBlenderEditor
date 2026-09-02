"""What the ``.blend`` (a cache) carries about its document. Component payloads are stored as a
JSON string for DISPLAY ONLY and never written back: ID property groups normalize types
(``int`` -> ``float``, tuple -> list), and "nearly the same value" is a bug in data promised
verbatim. The stamp is an undo-tracked ID property, which is #31.
"""

from __future__ import annotations

import json
import os

import bpy

__all__ = [
    "DocumentState",
    "clear_object",
    "component_json",
    "guid_of",
    "object_with_guid",
    "prefab_of",
    "read_state",
    "stamp_of",
    "tag_object",
    "tag_prefab",
    "write_state",
]

GUID_KEY = "paradise_guid"

#: Set on an object RESOLVED out of a prefab: saving one back would flatten the instance.
DERIVED_KEY = "paradise_derived"

#: The object's components as a JSON string. Read-only display data; never written back.
COMPONENTS_KEY = "paradise_components"

#: The prefab a NEW instance instantiates; an object from the document keeps its reference in
#: the file, but a new one has no entry to carry it from.
PREFAB_KEY = "paradise_prefab"

SCENE_PATH_KEY = "paradise_scene_path"

STAMP_KEY = "paradise_scene_stamp"


class DocumentState:
    """The link between a Blender scene and the document it came from."""

    def __init__(self, path: str, stamp: str) -> None:
        self.path = path
        self.stamp = stamp

    @property
    def is_stale(self) -> bool:
        """Whether the document changed on disk since it was read."""
        return stamp_of(self.path) != self.stamp


def stamp_of(path: str) -> str:
    """``"<mtime>:<size>"``, or ``""`` when the file is gone."""
    try:
        info = os.stat(path)
    except OSError:
        return ""
    return f"{info.st_mtime_ns}:{info.st_size}"


def write_state(scene: bpy.types.Scene, path: str) -> DocumentState:
    """Record that ``scene`` now reflects the document at ``path``."""
    state = DocumentState(os.path.abspath(path), stamp_of(path))
    scene[SCENE_PATH_KEY] = state.path
    scene[STAMP_KEY] = state.stamp
    return state


def read_state(scene: bpy.types.Scene) -> DocumentState | None:
    """The document this scene was materialized from, or ``None`` if it was not."""
    path = scene.get(SCENE_PATH_KEY)
    if not isinstance(path, str) or not path:
        return None
    return DocumentState(path, scene.get(STAMP_KEY, ""))


def tag_object(obj: bpy.types.Object, guid: str, components: list) -> None:
    """Mark ``obj`` as standing for the document object ``guid``."""
    obj[GUID_KEY] = guid
    obj[COMPONENTS_KEY] = json.dumps(components, ensure_ascii=False)


def tag_prefab(obj: bpy.types.Object, guid: str, path: str) -> None:
    """Record that ``obj`` instantiates the prefab at ``path``."""
    obj[PREFAB_KEY] = json.dumps({"guid": guid, "path": path}, ensure_ascii=False)


def prefab_of(obj: bpy.types.Object):
    """The prefab reference recorded on ``obj``, or ``None``."""
    raw = obj.get(PREFAB_KEY)
    if not raw:
        return None
    try:
        stored = json.loads(raw)
    except json.JSONDecodeError:
        return None
    guid, path = stored.get("guid"), stored.get("path")
    return (guid, path) if guid and path else None


def clear_object(obj: bpy.types.Object) -> None:
    """Detach an object from the document -- it becomes ordinary Blender content."""
    for key in (GUID_KEY, COMPONENTS_KEY, PREFAB_KEY):
        if key in obj:
            del obj[key]


def guid_of(obj: bpy.types.Object) -> str | None:
    """The document identity of ``obj``, or ``None`` if it is not a document object."""
    guid = obj.get(GUID_KEY)
    return guid if isinstance(guid, str) and guid else None


def object_with_guid(scene: bpy.types.Scene, guid: str | None):
    """The Blender object for *guid*, or ``None``. Case-insensitive; nothing promises a case."""
    if not guid:
        return None
    needle = guid.lower()
    for obj in scene.collection.all_objects:
        found = guid_of(obj)
        if found is not None and found.lower() == needle:
            return obj
    return None


def is_derived(obj: bpy.types.Object) -> bool:
    """Whether this object came out of a prefab rather than out of the scene document."""
    return bool(obj.get(DERIVED_KEY))


def mark_derived(obj: bpy.types.Object) -> None:
    """Mark and LOCK a prefab-resolved object: a move would be silently lost on the next load."""
    obj[DERIVED_KEY] = True
    obj.lock_location = (True, True, True)
    obj.lock_rotation = (True, True, True)
    obj.lock_scale = (True, True, True)


def component_json(obj: bpy.types.Object) -> list:
    """The object's components, for the panel. Empty when it carries none or the data is junk."""
    raw = obj.get(COMPONENTS_KEY)
    if not isinstance(raw, str) or not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []
