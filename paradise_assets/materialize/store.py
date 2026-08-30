"""What the ``.blend`` carries about the document it was materialized from.

The ``.blend`` is a CACHE, not a second source of truth, so it stores the minimum that lets a
save find its way back: which document this scene came from, what that file looked like when we
read it, and which document object each Blender object IS.

**Component payloads are stored for DISPLAY ONLY.** The save path takes them from the re-read
document, never from here -- which is what makes a component this addon has never heard of
survive a round trip untouched. Storing them as a JSON string rather than as Blender ID property
groups is deliberate: Blender's ID property system normalizes types on its way in and out (an
``int`` can come back a ``float``, a tuple a list), and for data we promise to return verbatim
"nearly the same value" is a bug. A string is a string.
"""

from __future__ import annotations

import json
import os

import bpy

__all__ = [
    "SceneState",
    "clear_object",
    "component_json",
    "guid_of",
    "read_state",
    "stamp_of",
    "tag_object",
    "write_state",
]

#: The document object's identity, on the Blender object that stands for it.
GUID_KEY = "paradise_guid"

#: The object's components as a JSON string. Read-only display data; never written back.
COMPONENTS_KEY = "paradise_components"

#: Scene-level: the absolute path of the document this scene was materialized from.
SCENE_PATH_KEY = "paradise_scene_path"

#: Scene-level: ``"<mtime>:<size>"`` of that document when it was read.
STAMP_KEY = "paradise_scene_stamp"


class SceneState:
    """The link between a Blender scene and the document it came from."""

    def __init__(self, path: str, stamp: str) -> None:
        self.path = path
        self.stamp = stamp

    @property
    def is_stale(self) -> bool:
        """Whether the document changed on disk since it was read.

        A save against a stale stamp would clobber whatever made the change -- another tool, a
        hand edit, a `git pull`. The caller refuses and offers reload instead.
        """
        return stamp_of(self.path) != self.stamp


def stamp_of(path: str) -> str:
    """``(mtime, size)`` as a comparable string, or ``""`` when the file is gone.

    Two cheap facts rather than a hash: reading a 200 KB document to decide whether to read it is
    silly, and mtime-plus-size catches every edit a person or a tool actually makes.
    """
    try:
        info = os.stat(path)
    except OSError:
        return ""
    return f"{info.st_mtime_ns}:{info.st_size}"


def write_state(scene: bpy.types.Scene, path: str) -> SceneState:
    """Record that ``scene`` now reflects the document at ``path``."""
    state = SceneState(os.path.abspath(path), stamp_of(path))
    scene[SCENE_PATH_KEY] = state.path
    scene[STAMP_KEY] = state.stamp
    return state


def read_state(scene: bpy.types.Scene) -> SceneState | None:
    """The document this scene was materialized from, or ``None`` if it was not."""
    path = scene.get(SCENE_PATH_KEY)
    if not isinstance(path, str) or not path:
        return None
    return SceneState(path, scene.get(STAMP_KEY, ""))


def tag_object(obj: bpy.types.Object, guid: str, components: list) -> None:
    """Mark ``obj`` as standing for the document object ``guid``."""
    obj[GUID_KEY] = guid
    obj[COMPONENTS_KEY] = json.dumps(components, ensure_ascii=False)


def clear_object(obj: bpy.types.Object) -> None:
    """Detach an object from the document -- it becomes ordinary Blender content."""
    for key in (GUID_KEY, COMPONENTS_KEY):
        if key in obj:
            del obj[key]


def guid_of(obj: bpy.types.Object) -> str | None:
    """The document identity of ``obj``, or ``None`` if it is not a document object."""
    guid = obj.get(GUID_KEY)
    return guid if isinstance(guid, str) and guid else None


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
