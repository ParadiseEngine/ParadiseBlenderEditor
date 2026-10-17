"""A fresh identity for a duplicated document object.

Every Blender copy gesture -- Shift+D, Alt+D linked duplicates, Ctrl+C/Ctrl+V -- copies ID
properties, so the copy arrives claiming the original's ``paradise_guid`` and the save used to
refuse both: it could not know which object the author meant to keep. At the moment of
duplication that is unambiguous -- the copy is the object that was just created -- so a
``depsgraph_update_post`` handler watches for an identity that is no longer unique and a
``bpy.app.timers`` callback re-mints the claimant it does not already know (mutating inside the
depsgraph callback is undefined behaviour; ``dropped.py`` is the prior art).

Attribution is by MEMORY, not guesswork: while an identity is unique the pass records which
object holds it (:data:`_KNOWN`, by name with the pointer as fallback -- undo restores names
wholesale, while pointers are allocation artifacts a memfile undo may reshuffle). When a second
claimant appears, the one already known is the original. A clash the pass cannot attribute --
the handler was disabled, or both claimants appeared between passes -- is left alone, and
``save._refuse_duplicate_identities`` remains as the safety net that names the objects.

The minted guid is REPEATABLE for the same copy: ``uuid5`` over the original's identity and the
copy's (unique) Blender name, not a bare ``uuid4``. Undo restores the clash with the same name,
so the re-mint after undo/redo produces the guid the copy already had -- the document's entries
do not churn every time the author steps across the duplicate.

A copy of a resolved prefab child (``paradise_derived``) is NOT re-minted: it is a second claim
on one prefab child, not a new object, and ``save._refuse_duplicate_derived`` keeps refusing it
-- the prefab decides what is in an instance.
"""

from __future__ import annotations

import uuid

import bpy

from . import edits as component_edits
from .document import guid as document_guid
from .document import overrides, well_known
from .materialize import store

__all__ = ["register_handler", "settle", "unregister_handler"]

#: Who holds each identity, ``(scene pointer, lowercase guid) -> (object name, object
#: pointer)``. Refreshed while the guid is unique, so a collision can be attributed to the
#: claimant the pass has never seen. Pruned as objects go away and CLEARED on file load: a new
#: file's objects may reuse freed memory, and a stale record could otherwise attribute a clash
#: the pass never saw.
_KNOWN: dict[tuple[int, str], tuple[str, int]] = {}

_scheduled = False

#: Components the copy must NOT inherit: identity is re-minted, the name travels as
#: ``NAME_KEY``, and the transform rides the object's channels.
_OWNED = frozenset({well_known.META_ID.lower(), well_known.TRANSFORM_ID.lower()})


def _claims(scene) -> dict[str, list]:
    """The scene's document objects grouped by identity (lowercase). Derived children are
    excluded: a duplicate of one is the save's ``_refuse_duplicate_derived`` case, not a new
    object."""
    claims: dict[str, list] = {}
    for obj in scene.collection.all_objects:
        guid = store.guid_of(obj)
        if guid is None or store.is_derived(obj):
            continue
        claims.setdefault(guid.lower(), []).append(obj)
    return claims


def _observe(scene) -> bool:
    """Refresh :data:`_KNOWN` from one scene; True when some identity has more than one
    claimant. A clashing guid keeps its PRE-clash record -- that is what attributes the copy
    when the timer runs."""
    if store.read_state(scene) is None:
        return False
    scene_key = scene.as_pointer()
    claims = _claims(scene)
    clashing = False
    for guid, objects in claims.items():
        if len(objects) == 1:
            _KNOWN[(scene_key, guid)] = (objects[0].name, objects[0].as_pointer())
        else:
            clashing = True
    present = {(scene_key, guid) for guid in claims}
    for key in list(_KNOWN):
        if key[0] == scene_key and key not in present:
            del _KNOWN[key]
    return clashing


def _original_of(scene_key: int, guid: str, objects: list):
    """The claimant :data:`_KNOWN` remembers, or ``None`` when no record singles one out.
    Names before pointers: both are recorded, but a rename between passes is the ordinary case
    and a recycled pointer after undo is the treacherous one."""
    record = _KNOWN.get((scene_key, guid))
    if record is None:
        return None
    name, pointer = record
    by_name = [obj for obj in objects if obj.name == name]
    if len(by_name) == 1:
        return by_name[0]
    by_pointer = [obj for obj in objects if obj.as_pointer() == pointer]
    return by_pointer[0] if len(by_pointer) == 1 else None


def _mint_for(obj, original_guid: str) -> str:
    """A guid that is repeatable for THIS copy: ``uuid5`` over the original's identity and the
    copy's Blender name, which is unique per file and is what undo/redo restores. Falls back to
    ``uuid4`` when the claimed identity is not a guid at all -- the save refuses such an
    original by name anyway."""
    namespace = document_guid.parse(original_guid)
    if namespace is None:
        return str(uuid.uuid4())
    return str(uuid.uuid5(namespace, f"paradise-duplicate:{obj.name}"))


def _remint(obj, original_guid: str) -> None:
    """Give a duplicated document object an identity of its own, keeping what the duplicate
    gesture promises: the components it shows, an instance's overrides, and unsaved edits."""
    shown = store.component_json(obj)
    pending_edits = component_edits.read(obj)
    pending_structure = component_edits.read_structure(obj)
    component_edits.clear(obj)

    if store.prefab_of(obj) is not None:
        _carry_instance_overrides(obj, shown, store.base_json(obj))
        # Everything it shows is the prefab's, so its own entry authors nothing yet -- exactly
        # like a placed instance, and what makes the reconstructed overrides save as OVERRIDES
        # rather than as a frozen copy of the prefab's payload.
        store.tag_authored(obj, [])
        # It materialized no children: keeping the original's list would tell the save the copy
        # showed children it does not have, and their absence would be written as
        # ``Dropped = true``. Child overrides (carriers) belong to the original's instance
        # relation and are not copied; the copy's children come from the prefab as authored.
        store.tag_children(obj, [])
    else:
        # A plain object's shown payload IS its entry, so queue it as additions: the copy
        # keeps the original's components, and its unsaved edits have something to land on.
        for component in shown:
            _queue_component(obj, component)

    _replay_pending(obj, pending_edits, pending_structure)
    store.tag_object(obj, _mint_for(obj, original_guid), shown)

    # The authored name belongs to the object, so the copy inherits it (the format allows two
    # objects one name); the SHOWN name is Blender's (``Crate.001``), or the save would record
    # the uniquified spelling as an author's rename (#32).
    authored = obj.get(store.NAME_KEY)
    store.tag_name(obj, authored if isinstance(authored, str) else None)


def _carry_instance_overrides(obj, shown: list, base: list) -> None:
    """Rebuild the original instance's root overrides on the copy as pending edits.

    The overrides live in the original's FILE entry, which a fresh guid no longer reaches; but
    the copy carries both the resolved payload (prefab + overrides) and the prefab's own
    baseline, and their per-field difference IS the override: the resolver merges per top-level
    field (``resolve._merge_data``), so ``overrides.differs`` recovers exactly the fields the
    instance set. Components the prefab does not have are instance additions; components the
    resolved view no longer shows are removals. An override pinning a field to the value the
    prefab already has is invisible in the diff and simply not copied.
    """
    base_by_id = {
        str(component["id"]).lower(): component.get("data")
        for component in base
        if isinstance(component, dict) and component.get("id")
    }
    shown_ids = set()
    for component in shown:
        if not isinstance(component, dict) or not component.get("id"):
            continue
        cid = str(component["id"])
        shown_ids.add(cid.lower())
        if cid.lower() in _OWNED:
            continue
        data = component.get("data")
        data = data if isinstance(data, dict) else {}
        base_data = base_by_id.get(cid.lower())
        if base_data is None:
            component_edits.add_component(
                obj, {"id": cid, "type": component.get("type"), "data": data})
            continue
        base_data = base_data if isinstance(base_data, dict) else {}
        for key in overrides.differs(base_data, data):
            if key in data:
                component_edits.set_field(obj, cid, key, data[key])
    for cid in base_by_id:
        if cid not in shown_ids and cid not in _OWNED:
            component_edits.remove_component(obj, cid)


def _queue_component(obj, component) -> None:
    """Queue one shown component as an addition on a plain object's copy."""
    if not isinstance(component, dict) or not component.get("id"):
        return
    cid = str(component["id"])
    if cid.lower() in _OWNED or component.get("removed"):
        return
    data = component.get("data")
    component_edits.add_component(
        obj, {"id": cid, "type": component.get("type"), "data": data if isinstance(data, dict) else {}})


def _replay_pending(obj, pending_edits: dict, pending_structure: dict) -> None:
    """The original's unsaved edits, re-queued over the reconstructed state: they are the
    newest author intent, so they win over anything the copy inherited. Removes come last:
    ``remove_component`` drops a pending add and clears the component's field edits, which is
    the precedence the panel itself keeps."""
    for spec in pending_structure["added"]:
        component_edits.add_component(obj, spec)
    for cid, fields in pending_edits.items():
        for path, value in fields.items():
            component_edits.set_field(obj, cid, path, value)
    for spec in pending_structure["reverted"]:
        component_edits.revert_field(obj, spec["id"], spec.get("path", ""))
    for cid in pending_structure["removed"]:
        component_edits.remove_component(obj, cid)


def _remint_pending() -> None:
    """The timer body: re-mint every claimant :data:`_KNOWN` does not recognize."""
    global _scheduled
    _scheduled = False
    live = {scene.as_pointer() for scene in bpy.data.scenes}
    for key in list(_KNOWN):
        if key[0] not in live:
            del _KNOWN[key]
    for scene in bpy.data.scenes:
        if store.read_state(scene) is None:
            continue
        scene_key = scene.as_pointer()
        for guid, objects in _claims(scene).items():
            if len(objects) < 2:
                continue
            original = _original_of(scene_key, guid, objects)
            if original is None:
                # Not attributable to a copy (both appeared between passes, or the handler was
                # off): leave it for the save to refuse by name.
                continue
            for obj in objects:
                if obj is not original:
                    _remint(obj, guid)
                    _KNOWN[(scene_key, store.guid_of(obj).lower())] = (obj.name, obj.as_pointer())


def settle() -> None:
    """One synchronous detection + re-mint pass: the handler's work on demand, so a test does
    not have to pump Blender's timer queue."""
    global _scheduled
    _scheduled = False
    for scene in bpy.data.scenes:
        _observe(scene)
    _remint_pending()


@bpy.app.handlers.persistent
def _on_depsgraph_update(scene, depsgraph) -> None:
    global _scheduled
    # Only the scene that changed, and non-document scenes skip immediately -- the early-out
    # dropped.py relies on. Every copy gesture creates objects, so a clash always arrives with
    # an update on the scene it happened in.
    if _scheduled:
        return
    if _observe(scene):
        _scheduled = True
        bpy.app.timers.register(_remint_pending, first_interval=0.0)


@bpy.app.handlers.persistent
def _on_load_post(*_) -> None:
    """A new file invalidates everything memory-keyed, and a pending timer may have died with
    the old file: start clean rather than attribute a clash to a recycled pointer."""
    global _scheduled
    _KNOWN.clear()
    _scheduled = False


def register_handler() -> None:
    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)


def unregister_handler() -> None:
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
