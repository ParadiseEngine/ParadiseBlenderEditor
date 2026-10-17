"""A fresh identity for a duplicated document object.

Every Blender copy gesture -- Shift+D, Alt+D linked duplicates, Ctrl+C/Ctrl+V -- copies ID
properties, so the copy arrives claiming the original's ``paradise_guid`` and the save used to
refuse both: it could not know which object the author meant to keep. At the moment of
duplication that is unambiguous -- the copy is the object that was just created -- so a
``depsgraph_update_post`` handler watches for an identity that is no longer unique and a
``bpy.app.timers`` callback re-mints the claimant it does not already know (mutating inside the
depsgraph callback is undefined behaviour; ``dropped.py`` is the prior art).

Attribution is by MEMORY, not guesswork: every pass records which object holds each unique
identity (:data:`_KNOWN`), so when a second claimant appears the one already known is the
original. A clash the pass cannot attribute -- the handler was disabled, or both claimants
appeared between passes -- is left alone, and ``save._refuse_duplicate_identities`` remains as
the safety net that names the objects.

A copy of a resolved prefab child (``paradise_derived``) is NOT re-minted: it is a second claim
on one prefab child, not a new object, and ``save._refuse_duplicate_derived`` keeps refusing it
-- the prefab decides what is in an instance.
"""

from __future__ import annotations

import uuid

import bpy

from . import edits as component_edits
from .materialize import store

__all__ = ["register_handler", "settle", "unregister_handler"]

#: Who holds each identity, ``(scene pointer, lowercase guid) -> object pointer``. Refreshed on
#: every pass while the guid is unique, so a collision can be attributed to the claimant the
#: pass has never seen. Pointers, not names: the author can rename the copy, and names
#: uniquify. Pruned as scenes and objects go away, and stale entries only ever cost an
#: unattributed clash -- the save's refusal is the net under this.
_KNOWN: dict[tuple[int, str], int] = {}

_scheduled = False


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


def _observe() -> bool:
    """Refresh :data:`_KNOWN` from the live scenes; True when some identity has more than one
    claimant. A clashing guid keeps its PRE-collision entry -- that is what attributes the
    copy when the timer runs."""
    live = {scene.as_pointer() for scene in bpy.data.scenes}
    for key in list(_KNOWN):
        if key[0] not in live:
            del _KNOWN[key]

    clashing = False
    for scene in bpy.data.scenes:
        if store.read_state(scene) is None:
            continue
        scene_key = scene.as_pointer()
        claims = _claims(scene)
        for guid, objects in claims.items():
            if len(objects) == 1:
                _KNOWN[(scene_key, guid)] = objects[0].as_pointer()
            else:
                clashing = True
        present = {(scene_key, guid) for guid in claims}
        for key in list(_KNOWN):
            if key[0] == scene_key and key not in present:
                del _KNOWN[key]
    return clashing


def _remint(obj) -> None:
    """Give a duplicated document object an identity of its own.

    A fresh ``uuid4``, as ``instancing.add_instance`` mints: object identity inside a document
    is the document's own, and CONVENTIONS.md's "never minted" rule is about file sidecars.
    """
    fresh = str(uuid.uuid4())
    if store.prefab_of(obj) is not None:
        # A second instance of the SAME prefab. Its own entry is meta, transform and the
        # reference, so it shows what the prefab alone says, authors nothing yet and has
        # materialized no children -- exactly what `add_instance` tags. Keeping the original's
        # CHILDREN list would tell the save the copy showed children it does not have, and the
        # save would write their absence as `Dropped = true`.
        store.tag_object(obj, fresh, store.base_json(obj))
        store.tag_authored(obj, [])
        store.tag_children(obj, [])
    else:
        store.tag_object(obj, fresh, store.component_json(obj))

    # The pending-edit overlay speaks about the ORIGINAL's file entry, which the copy does not
    # have: kept, an edit to a component only that entry carries is dropped at the save with a
    # warning, which punishes the author for duplicating. The copy starts unedited.
    component_edits.clear(obj)

    # The authored name belongs to the object, so the copy inherits it (the format allows two
    # objects one name); the SHOWN name is Blender's (``Crate.001``), or the save would record
    # the uniquified spelling as an author's rename (#32).
    authored = obj.get(store.NAME_KEY)
    store.tag_name(obj, authored if isinstance(authored, str) else None)


def _remint_pending() -> None:
    """The timer body: re-mint every claimant :data:`_KNOWN` does not recognize."""
    global _scheduled
    _scheduled = False
    for scene in bpy.data.scenes:
        if store.read_state(scene) is None:
            continue
        scene_key = scene.as_pointer()
        for guid, objects in _claims(scene).items():
            if len(objects) < 2:
                continue
            known = _KNOWN.get((scene_key, guid))
            originals = [obj for obj in objects if obj.as_pointer() == known]
            if len(originals) != 1:
                # Not attributable to a copy (both appeared between passes, or the handler was
                # off): leave it for the save to refuse by name.
                continue
            for obj in objects:
                if obj is not originals[0]:
                    _remint(obj)


def settle() -> None:
    """One synchronous detection + re-mint pass: the handler's work on demand, so a test does
    not have to pump Blender's timer queue."""
    global _scheduled
    _scheduled = False
    _observe()
    _remint_pending()


@bpy.app.handlers.persistent
def _on_depsgraph_update(scene, depsgraph) -> None:
    global _scheduled
    # _observe walks every document scene's objects; so does dropped.py's check, and this runs
    # on the same event.
    if _scheduled:
        return
    if _observe():
        _scheduled = True
        bpy.app.timers.register(_remint_pending, first_interval=0.0)


def register_handler() -> None:
    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)


def unregister_handler() -> None:
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
