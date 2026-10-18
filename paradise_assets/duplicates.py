"""A fresh identity for a duplicated document object.

Every Blender copy gesture -- Shift+D, Alt+D linked duplicates, Ctrl+C/Ctrl+V -- copies ID
properties, so the copy arrives claiming the original's ``paradise_guid`` and the save used to
refuse both: it could not know which object the author meant to keep. At the moment of
duplication that is unambiguous -- the copy is the object that was just created -- so a
``depsgraph_update_post`` handler watches for an identity that is no longer unique and a
``bpy.app.timers`` callback re-mints the claimant it does not already know (mutating inside the
depsgraph callback is undefined behaviour; ``dropped.py`` is the prior art).

Attribution is by MEMORY, not guesswork: while an identity is unique the pass records which
object holds it (:data:`_KNOWN`, by ``ID.session_uid``). When a second claimant appears, the one
already known is the original. ``session_uid`` is the key because it is the one handle that
fits: undo/redo restores it (pointers are reallocated by a memfile undo), a rename does not
change it (names do), and Blender never hands it to another ID within a session (``.001``
names are reused the moment they are free). A clash the pass cannot attribute -- the handler
was disabled, or both claimants appeared between passes -- is left alone, and
``save._refuse_duplicate_identities`` remains as the safety net that names the objects.

The minted guid is a ``uuid4`` REMEMBERED per copy (:data:`_MINTED`, by the original's
identity and the copy's ``session_uid``). The re-mint runs in a timer after the operator's undo
push, so it never enters the undo history: stepping back across the duplicate restores the
clash, and the re-mint after that undo hands the restored copy the guid it already had instead
of churning the document's entries. A deleted copy, or a fresh duplicate of the same original,
is a new ID with a new ``session_uid`` and therefore a new guid -- an identity the document
once held is never given to an unrelated object.

A copy of a resolved prefab child (``paradise_derived``) is NOT re-minted as a new object --
it is a second claim on one prefab child, and the prefab decides what is in an instance. The
one exception is the duplicate that took the WHOLE subtree: Shift+D on an instance together
with its resolved children copies the derived markers too, and Blender re-parents the child
copies within the copy's subtree. Those copies are ADOPTED by the instance copy -- their
``(instance, local)`` carrier address is rewritten to the copy's fresh guid and their own
guid is re-minted exactly as the resolver will mint the copy's materialization -- so the copy
keeps the original's child overrides. A child copied WITHOUT its parent chain stays parented
under the ORIGINAL's subtree; it is not adopted, and ``save._refuse_duplicate_derived`` keeps
refusing it.
"""

from __future__ import annotations

import uuid

import bpy

from . import edits as component_edits
from .document import overrides, resolve, well_known
from .materialize import store

__all__ = ["register_handler", "settle", "unregister_handler"]

#: Who holds each identity, ``(scene session_uid, lowercase guid) -> object session_uid``.
#: Refreshed while the guid is unique, so a collision can be attributed to the claimant the
#: pass has never seen. Pruned as scenes and identities go away, and cleared on file load.
_KNOWN: dict[tuple[int, str], int] = {}

#: The guid each copy was minted, ``(lowercase original guid, copy session_uid) -> guid``, so an
#: undo that restores the clash re-mints the guid the copy already had. Cleared on file load,
#: which gives every ID a new ``session_uid``.
_MINTED: dict[tuple[str, int], str] = {}

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
    scene_key = scene.session_uid
    claims = _claims(scene)
    clashing = False
    for guid, objects in claims.items():
        if len(objects) == 1:
            _KNOWN[(scene_key, guid)] = objects[0].session_uid
        else:
            clashing = True
    present = {(scene_key, guid) for guid in claims}
    for key in list(_KNOWN):
        if key[0] == scene_key and key not in present:
            del _KNOWN[key]
    return clashing


def _original_of(scene_key: int, guid: str, objects: list):
    """The claimant :data:`_KNOWN` remembers, or ``None`` when no record singles one out."""
    holder = _KNOWN.get((scene_key, guid))
    if holder is None:
        return None
    found = [obj for obj in objects if obj.session_uid == holder]
    return found[0] if len(found) == 1 else None


def _mint_for(obj, original_guid: str, claimed: set[str]) -> str:
    """The guid this copy was minted before -- an undo restored the clash -- or a fresh
    ``uuid4``. Never one another object in the scene already claims: a remembered guid that is
    taken again is replaced rather than handed out twice."""
    key = (original_guid, obj.session_uid)
    guid = _MINTED.get(key)
    if guid is None or guid.lower() in claimed:
        guid = str(uuid.uuid4())
        _MINTED[key] = guid
    return guid


def _remint(obj, original_guid: str, claimed: set[str]) -> None:
    """Give a duplicated document object an identity of its own, keeping what the duplicate
    gesture promises: the components it shows, an instance's overrides, and unsaved edits.

    The copy follows the convention of every other new object: its snapshot is what the FILE
    will have before this session's edits -- nothing for a plain object (as a newly adopted
    group), the prefab's root for an instance (as a placed one) -- and everything it inherits
    is pending overlay on top. The panel draws snapshot + overlay and the save writes a fresh
    entry from the overlay, so the two agree under every overlay gesture: discarding the edits
    leaves an empty object or a bare instance in the panel AND in the file, and removing an
    inherited component hides it in both. Keeping the original's payload as the snapshot
    would have the panel go on showing what the save no longer writes.
    """
    shown = store.component_json(obj)
    pending_edits = component_edits.read(obj)
    pending_structure = component_edits.read_structure(obj)
    component_edits.clear(obj)

    if store.prefab_of(obj) is not None:
        base = store.base_json(obj)
        _carry_instance_overrides(obj, shown, base)
        # Everything it shows is the prefab's, so its own entry authors nothing yet -- exactly
        # like a placed instance, and what makes the reconstructed overrides save as OVERRIDES
        # rather than as a frozen copy of the prefab's payload.
        store.tag_authored(obj, [])
        # It materialized no children of its own: keeping the original's list would tell the
        # save the copy showed children it does not have, and their absence would be written as
        # ``Dropped = true``. Children duplicated WITH the instance are adopted below, and only
        # those are recorded; every other child comes from the prefab as authored on the save's
        # refresh.
        store.tag_children(obj, [])
        snapshot = base
    else:
        # A plain object's shown payload IS its entry, so queue it as additions: the copy
        # keeps the original's components, and its unsaved edits have something to land on.
        for component in shown:
            _queue_component(obj, component)
        snapshot = []

    _replay_pending(obj, pending_edits, pending_structure)
    minted = _mint_for(obj, original_guid, claimed)
    store.tag_object(obj, minted, snapshot)
    if store.prefab_of(obj) is not None:
        adopted = _adopt_child_copies(obj, original_guid, minted)
        if adopted:
            store.tag_children(obj, adopted)

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
            # `differs` also yields fields only the BASE has, but the resolver's merge never
            # drops a field, so the resolved payload cannot lack one -- such a key cannot be
            # part of the override and is skipped.
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


def _adopt_child_copies(instance_copy, original_guid: str, copy_guid: str) -> list[str]:
    """Re-address the copies of the original instance's resolved children to the copy.

    Duplicating an instance TOGETHER with its children copies the derived markers: each child
    copy arrives claiming the ORIGINAL instance's ``(instance, local)`` address, one claim too
    many. A copy whose whole parent chain was duplicated with it -- Blender re-parents the
    copies within the new subtree, so the chain of copies reaches the instance copy -- is
    adopted instead of refused: its carrier address moves to ``(copy guid, local)``, and its
    guid is re-minted as ``uuid5(copy guid, local)``, the identity the copy's own
    materialization will have (``resolve.mint_child_guid`` -- deterministic, so an undo that
    restores the clash re-adopts the same guids). Its snapshot becomes what that
    materialization would show -- the PREFAB's baseline, re-parented into the copy's subtree --
    so a child copy still standing where the ORIGINAL's override put it reads as moved, and the
    save writes the move as a carrier on the COPY's entry: the original's child overrides
    transfer. Component overrides on children do not (the gesture copies no payload of its own),
    and snapshot := baseline is what keeps the panel and the saved file agreeing about that.
    Nested content (``own = False``) keeps its flattened local, the same address
    ``overrides.locals_of`` computes.

    A child copied WITHOUT its parent chain stays parented under the ORIGINAL's subtree and is
    NOT adopted: half a subtree cannot move house, and the save keeps refusing it. The returned
    locals are exactly the adopted ones, so nothing the gesture did not copy is recorded as
    shown -- that record drives the "author deleted this child" logic, and over-recording
    writes ``Dropped = true``.
    """
    lowered = original_guid.lower()
    candidates = [
        obj for obj in instance_copy.children_recursive
        if store.is_derived(obj)
        and (address := store.local_of(obj)) is not None
        and address[0].lower() == lowered
    ]
    members = {obj.as_pointer() for obj in candidates}

    def reaches_copy(obj) -> bool:
        parent = obj.parent
        while parent is not None and parent is not instance_copy:
            if parent.as_pointer() not in members:
                return False
            parent = parent.parent
        return parent is instance_copy

    adopted = []
    for obj in candidates:
        if not reaches_copy(obj):
            continue
        _instance, local, own = store.local_of(obj)
        parent = obj.parent
        parent_guid = (
            copy_guid if parent is instance_copy
            else resolve.mint_child_guid(copy_guid, store.local_of(parent)[1])
        )
        guid = resolve.mint_child_guid(copy_guid, local)
        store.tag_local(obj, copy_guid, local, own)
        base = _readdressed(store.base_json(obj), guid, parent_guid)
        store.tag_object(obj, guid, base)
        store.tag_base(obj, base)
        adopted.append(local)
    return adopted


def _readdressed(components: list, guid: str, parent_guid: str) -> list:
    """The payload with meta identity re-minted and re-parented to the copy's subtree, so the
    save's re-parenting check sees the address the copy's own materialization would produce."""
    for component in components:
        if not isinstance(component, dict):
            continue
        if str(component.get("id", "")).lower() != well_known.META_ID:
            continue
        data = component.get("data")
        if isinstance(data, dict):
            data[well_known.GUID] = guid
            data[well_known.PARENT] = parent_guid
    return components


def _remint_pending() -> None:
    """The timer body: re-mint every claimant :data:`_KNOWN` does not recognize."""
    live = {scene.session_uid for scene in bpy.data.scenes}
    for key in list(_KNOWN):
        if key[0] not in live:
            del _KNOWN[key]
    for scene in bpy.data.scenes:
        if store.read_state(scene) is None:
            continue
        scene_key = scene.session_uid
        claims = _claims(scene)
        claimed = set(claims)
        for guid, objects in claims.items():
            if len(objects) < 2:
                continue
            original = _original_of(scene_key, guid, objects)
            if original is None:
                # Not attributable to a copy (both appeared between passes, or the handler was
                # off): leave it for the save to refuse by name.
                continue
            for obj in objects:
                if obj is not original:
                    _remint(obj, guid, claimed)
                    minted = store.guid_of(obj).lower()
                    claimed.add(minted)
                    _KNOWN[(scene_key, minted)] = obj.session_uid


def settle() -> None:
    """One synchronous detection + re-mint pass: the handler's work on demand, so a test does
    not have to pump Blender's timer queue. A queued timer is cancelled first: its work has
    just been done, and the handler's ``is_registered`` gate would otherwise swallow the next
    pass's scan."""
    if bpy.app.timers.is_registered(_remint_pending):
        bpy.app.timers.unregister(_remint_pending)
    _observe_all()
    _remint_pending()


def _observe_all() -> None:
    """Record every scene's holders, re-minting nothing. Also the one-shot timer after a file
    load or addon registration: neither is a depsgraph update this handler sees, and without a
    record the first duplicate made afterwards would be unattributable."""
    for scene in bpy.data.scenes:
        _observe(scene)


def _no_new_objects(depsgraph) -> bool:
    """True while nothing was added or reshaped: every update is an Object with no geometry
    change. Measured on Blender 5.2: a transform drag updates only the moved Object (transform
    flag), and a material tweak adds its shading flag, while linking an object -- the first
    half of every copy gesture -- updates the new Object WITH its geometry flag, plus its
    collection and the scene. So geometry on an Object, or any non-Object entry, means
    "look"; a move or a material change does not."""
    updates = depsgraph.updates
    if not updates:
        return False
    return all(
        isinstance(update.id, bpy.types.Object) and not update.is_updated_geometry
        for update in updates
    )


@bpy.app.handlers.persistent
def _on_depsgraph_update(scene, depsgraph) -> None:
    # Only the scene that changed, and non-document scenes skip immediately -- the early-out
    # dropped.py relies on. Every copy gesture creates objects, so a clash always arrives with
    # an update on the scene it happened in; an undo that restores one re-evaluates the scene
    # (collection included), which is an update here as well. A queued re-mint already covers
    # whatever this pass would find.
    if bpy.app.timers.is_registered(_remint_pending) or _no_new_objects(depsgraph):
        return
    if _observe(scene):
        bpy.app.timers.register(_remint_pending, first_interval=0.0)


def _schedule_observe_all() -> None:
    # Persistent: a load clears ordinary timers, and this one is registered from load_post.
    # Deferred rather than run here: other load_post handlers (watch.py's rematerialization)
    # may still rebuild the objects, and registration runs with restricted ``bpy.data``.
    if not bpy.app.timers.is_registered(_observe_all):
        bpy.app.timers.register(_observe_all, first_interval=0.0, persistent=True)


@bpy.app.handlers.persistent
def _on_load_post(*_) -> None:
    """A new file gives every ID a new ``session_uid``, so every record is dead; the file's
    holders are recorded again once the load settles."""
    _KNOWN.clear()
    _MINTED.clear()
    _schedule_observe_all()


def register_handler() -> None:
    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)
    # Enabling the addon onto an open workfile fires no load_post.
    _schedule_observe_all()


def unregister_handler() -> None:
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    # A queued pass would otherwise run this module's callback after the addon is gone (or
    # reloaded).
    for timer in (_remint_pending, _observe_all):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)
