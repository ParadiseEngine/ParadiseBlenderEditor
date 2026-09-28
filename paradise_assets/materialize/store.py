"""What the ``.blend`` (a cache) carries about its document. Component payloads are stored as a
JSON string for DISPLAY ONLY and never written back: ID property groups normalize types
(``int`` -> ``float``, tuple -> list), and "nearly the same value" is a bug in data promised
verbatim. The stamp lives in the scene for the workfile's sake and in :data:`_STAMPS` for this
process's, since the scene copy is undo-tracked.
"""

from __future__ import annotations

import json
import os

import bpy

from ..document import guid as document_guid
from ..document import project

__all__ = [
    "DocumentState",
    "base_json",
    "clear_object",
    "component_json",
    "document_name",
    "guid_of",
    "local_of",
    "mark",
    "object_with_guid",
    "prefab_of",
    "project_of",
    "read_state",
    "resolved_children",
    "stamp_of",
    "strip_marks",
    "tag_base",
    "tag_children",
    "tag_local",
    "tag_name",
    "tag_object",
    "tag_prefab",
    "write_state",
]

GUID_KEY = "paradise_guid"

#: Set on an object RESOLVED out of a prefab: saving one back would flatten the instance.
DERIVED_KEY = "paradise_derived"

#: JSON list of component ids the object's OWN file entry carries. Set on an instance, whose
#: displayed payload folds the prefab's components in; absent on an ordinary object, where the
#: payload is the entry.
AUTHORED_KEY = "paradise_authored_components"

#: The object's components as a JSON string. Read-only display data; never written back.
COMPONENTS_KEY = "paradise_components"

#: The prefab a NEW instance instantiates; an object from the document keeps its reference in
#: the file, but a new one has no entry to carry it from.
PREFAB_KEY = "paradise_prefab"

#: On a resolved child: how a carrier addresses it -- ``{instance, local, own}``. ``uuid5`` does
#: not invert, so the prefab-LOCAL guid a carrier has to spell cannot be recovered from the
#: minted one and is recorded here instead. ``own`` is False for a child that came out of a
#: prefab nested inside this one: overridable, but nothing can write that override into a file.
LOCAL_KEY = "paradise_prefab_local"

#: The object's components as the PREFAB alone says them, JSON. The panel diffs the shown value
#: against this to find what an instance overrides, and the save prunes an override that has
#: come back to it. NOT the same thing as :data:`COMPONENTS_KEY`, which is the RESOLVED value
#: (prefab plus overrides) and is what a move is measured against -- a child a carrier already
#: moved must not read as having moved again.
BASE_KEY = "paradise_base"

#: On an instance: the prefab-local guids the load actually materialized for it, JSON.
#: The only way a save can tell "the author deleted this child" from "the prefab no longer has
#: it" without re-reading the prefab -- which would turn one unreadable prefab into a level full
#: of ``Dropped = true`` written by a save that reported success.
CHILDREN_KEY = "paradise_resolved_children"

#: The document's ``meta.Name`` (absent when it has none) and the name Blender gave the object
#: when it was materialized. Blender uniquifies (``Wall.001``) and truncates in one namespace
#: shared with every imported GLB node, so ``obj.name`` alone cannot say whether the AUTHOR
#: renamed anything (#32).
NAME_KEY = "paradise_name"
SHOWN_NAME_KEY = "paradise_shown_name"

SCENE_PATH_KEY = "paradise_scene_path"

STAMP_KEY = "paradise_scene_stamp"

#: Stamps of documents written by THIS process, keyed by normalised path. The scene property
#: is undo-tracked, so Ctrl+Z after a save resurrected the pre-save stamp and the next save was
#: refused as "changed on disk" by the session's own write (#31). This table is not undone.
_STAMPS: dict[str, str] = {}


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
    _STAMPS[os.path.normcase(state.path)] = state.stamp
    return state


def read_state(scene: bpy.types.Scene) -> DocumentState | None:
    """The document this scene was materialized from, or ``None`` if it was not. The stamp
    is the last one this process recorded for the path where there is one (undo cannot roll
    that back), else the workfile's."""
    path = scene.get(SCENE_PATH_KEY)
    if not isinstance(path, str) or not path:
        return None
    stamp = _STAMPS.get(os.path.normcase(os.path.abspath(path)), scene.get(STAMP_KEY, ""))
    return DocumentState(path, stamp)


def project_of(scene: bpy.types.Scene) -> project.ProjectLayout | None:
    """The asset project this session belongs to: the open document's, else the one containing
    the ``.blend`` itself.

    The fallback is what makes the project-level actions (build, verify, watch) reachable before
    any document is open -- a workfile under ``.editor/blend/`` is already inside its project,
    and so is a ``.blend`` an author keeps beside their game. Without it those buttons could only
    be offered from inside a document, which is the one place they are least needed.
    """
    state = read_state(scene)
    if state is not None:
        return project.locate(state.path)
    return project.locate(bpy.data.filepath) if bpy.data.filepath else None


def tag_object(obj: bpy.types.Object, guid: str, components: list) -> None:
    """Mark ``obj`` as standing for the document object ``guid``."""
    obj[GUID_KEY] = guid
    obj[COMPONENTS_KEY] = json.dumps(components, ensure_ascii=False)


#: What the Outliner is told, appended to the Blender name and never written to the document.
#: Blender exposes no per-object Outliner icon, so the name is the only channel there is.
#: A SUFFIX rather than a prefix, so sibling rows keep sorting alphabetically.
MARK_INSTANCE = " \u25b8"
MARK_DERIVED = " \u00b7"
MARK_OVERRIDDEN = "*"

_MARK_CHARS = "\u25b8\u00b7* "


def mark(obj: bpy.types.Object, authored: str | None, suffix: str = "") -> None:
    """Name ``obj`` for the Outliner and record what the document should say, in one call.

    ONE writer, on purpose. ``document_name`` trusts :data:`SHOWN_NAME_KEY` to tell an author's
    rename from Blender's uniquifying (#32); a caller that set ``obj.name`` for a mark and forgot
    to record it would make ``document_name`` return the DECORATED string, and the next save
    would write ``Crate \u25b8`` into ``assets/`` -- where the next load would decorate it again.
    """
    # `strip_marks` on the fallback, not `obj.name` raw: this runs again on every save, and the
    # object is already decorated by then -- appending to that gives `Crate \u25b8 \u25b8`.
    wanted = f"{authored if authored is not None else strip_marks(obj.name)}{suffix}"
    if obj.name != wanted:
        obj.name = wanted
    tag_name(obj, authored)


def tag_name(obj: bpy.types.Object, authored: str | None) -> None:
    """Record the document's name for ``obj`` and the name Blender is showing for it now."""
    if authored is None:
        if NAME_KEY in obj:
            del obj[NAME_KEY]
    else:
        obj[NAME_KEY] = authored
    obj[SHOWN_NAME_KEY] = obj.name


def strip_marks(name: str) -> str:
    """``name`` without the Outliner marks, keeping Blender's ``.001`` where it put one.

    Only the rename path needs this, and only because an author editing a decorated row usually
    keeps the decoration. It never removes ``.NNN``: the format allows two objects one name, and
    ``document_name`` already writes ``Wall.001`` when that is what the author typed.
    """
    head, dot, tail = name.rpartition(".")
    if dot and tail.isdigit() and head:
        return strip_marks(head) + dot + tail
    return name.rstrip(_MARK_CHARS) or name


def document_name(obj: bpy.types.Object) -> str | None:
    """What ``meta.Name`` should say: the author's rename when there was one, else the authored
    name untouched -- ``Wall.001`` is Blender's, not the author's, and the format allows two
    objects one name. A rename keeps whatever mark the author left on it out of the document."""
    shown = obj.get(SHOWN_NAME_KEY)
    if isinstance(shown, str) and shown == obj.name:
        authored = obj.get(NAME_KEY)
        return authored if isinstance(authored, str) else None
    return strip_marks(obj.name)


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


#: Every marker this module writes. `clear_object` has to take all of them: one left behind is
#: an object that is not in the document but still answers a question about it -- a stale
#: `DERIVED_KEY` hides it from the save for good.
_MARKERS = (
    GUID_KEY, COMPONENTS_KEY, PREFAB_KEY, NAME_KEY, SHOWN_NAME_KEY,
    DERIVED_KEY, AUTHORED_KEY, LOCAL_KEY, BASE_KEY, CHILDREN_KEY,
)


def clear_object(obj: bpy.types.Object) -> None:
    """Detach an object from the document -- it becomes ordinary Blender content."""
    for key in _MARKERS:
        if key in obj:
            del obj[key]


def guid_of(obj: bpy.types.Object) -> str | None:
    """The document identity of ``obj``, or ``None`` if it is not a document object. Canonical
    spelling, so it compares by value against a document's own identities (#30); a marker that
    is not a guid at all is returned as stored, for the save to refuse by name."""
    guid = obj.get(GUID_KEY)
    if not isinstance(guid, str) or not guid:
        return None
    return document_guid.canonical(guid) if document_guid.parse(guid) is not None else guid


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


def tag_authored(obj: bpy.types.Object, component_ids) -> None:
    """Record which components this instance's own entry authors (lower-cased ids)."""
    obj[AUTHORED_KEY] = json.dumps(sorted({str(c).lower() for c in component_ids}))


def authors(obj: bpy.types.Object, component_id: str) -> bool:
    """Whether the object's OWN file entry carries this component -- the only components the
    save writes for it. An ordinary object authors everything it shows; an instance only what
    its entry adds over the prefab; a derived child nothing."""
    if is_derived(obj):
        return False
    raw = obj.get(AUTHORED_KEY)
    if not isinstance(raw, str):
        return True
    try:
        return component_id.lower() in json.loads(raw)
    except json.JSONDecodeError:
        return True


def tag_local(obj: bpy.types.Object, instance: str, local: str, own: bool) -> None:
    """Record how a carrier addresses this resolved child."""
    obj[LOCAL_KEY] = json.dumps(
        {"instance": instance, "local": local, "own": bool(own)}, ensure_ascii=False
    )


def local_of(obj: bpy.types.Object):
    """``(instance guid, prefab-local guid, own)`` for a resolved child, or ``None``."""
    stored = _json_object(obj, LOCAL_KEY)
    if stored is None:
        return None
    instance, local = stored.get("instance"), stored.get("local")
    if not instance or not local:
        return None
    return instance, local, bool(stored.get("own", True))


def tag_base(obj: bpy.types.Object, components: list) -> None:
    """Record what the PREFAB alone says about this object."""
    obj[BASE_KEY] = json.dumps(components, ensure_ascii=False)


def base_json(obj: bpy.types.Object) -> list:
    """The prefab's components for this object. Empty for an object no prefab authored, which
    is the right answer: it overrides nothing."""
    raw = obj.get(BASE_KEY)
    if not isinstance(raw, str) or not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def tag_children(obj: bpy.types.Object, locals_materialized) -> None:
    """Record which of the prefab's children this instance actually put in the scene."""
    obj[CHILDREN_KEY] = json.dumps(sorted({str(local) for local in locals_materialized}))


def resolved_children(obj: bpy.types.Object) -> set[str] | None:
    """The prefab-local guids this instance materialized, or ``None`` when it was never
    recorded. ``None`` is not an empty set: a workfile written before this was recorded knows
    nothing, and inferring "everything was deleted" from silence would drop a whole prefab."""
    raw = obj.get(CHILDREN_KEY)
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return {str(item) for item in parsed} if isinstance(parsed, list) else None


def _json_object(obj: bpy.types.Object, key: str) -> dict | None:
    raw = obj.get(key)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def is_derived(obj: bpy.types.Object) -> bool:
    """Whether this object came out of a prefab rather than out of the scene document."""
    return bool(obj.get(DERIVED_KEY))


def mark_derived(obj: bpy.types.Object) -> None:
    """Mark a prefab-resolved object. It is MOVABLE: a move becomes an override carrier.

    The locks this used to set are cleared rather than merely not set. They live in the
    ``.blend``, so every working file written before overrides could be authored still carries
    them, and an author who has opened this level once would otherwise keep locked children with
    nothing on screen to say why.
    """
    obj[DERIVED_KEY] = True
    obj.lock_location = (False, False, False)
    obj.lock_rotation = (False, False, False)
    obj.lock_scale = (False, False, False)


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
