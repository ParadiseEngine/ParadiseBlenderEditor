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
    "DocumentState",
    "clear_object",
    "component_json",
    "guid_of",
    "prefab_of",
    "read_state",
    "stamp_of",
    "tag_object",
    "tag_prefab",
    "write_state",
]

#: The document object's identity, on the Blender object that stands for it.
GUID_KEY = "paradise_guid"

#: Set on an object that was RESOLVED out of a prefab rather than written in the scene.
#:
#: These exist only because the scene instantiates a prefab; the document has no entry for them,
#: and their identities are minted. Saving one back would write it into the scene as a plain
#: object -- flattening the instance and, on the next load, producing a duplicate. So they are
#: marked, locked in the viewport, and skipped by the save path entirely.
DERIVED_KEY = "paradise_derived"

#: The object's components as a JSON string. Read-only display data; never written back.
COMPONENTS_KEY = "paradise_components"

#: The prefab an object instantiates, as ``{"guid": …, "path": …}``.
#:
#: Only NEW instances need this. An object that came from the document keeps its prefab reference
#: in the file, and save carries that through untouched -- but an instance added here has no entry
#: to carry anything from, and without the marker the save would write it as a plain object and
#: lose the reference that makes it an instance.
PREFAB_KEY = "paradise_prefab"

#: Scene-level: the absolute path of the document this scene was materialized from.
SCENE_PATH_KEY = "paradise_scene_path"

#: Scene-level: ``"<mtime>:<size>"`` of that document when it was read.
STAMP_KEY = "paradise_scene_stamp"


class DocumentState:
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


def is_derived(obj: bpy.types.Object) -> bool:
    """Whether this object came out of a prefab rather than out of the scene document."""
    return bool(obj.get(DERIVED_KEY))


def mark_derived(obj: bpy.types.Object) -> None:
    """Mark and LOCK an object resolved from a prefab.

    Locked because the edit would be silently discarded: this addon writes prefab instances back
    as instances, so a moved child is lost on the next load with nothing to say why. Locking makes
    the constraint visible in the viewport instead of surprising somebody later.
    """
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
