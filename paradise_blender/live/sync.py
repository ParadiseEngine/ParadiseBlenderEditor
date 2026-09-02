"""Blender edits -> live-preview messages. ``depsgraph_update_post`` fires hundreds of times a
second during a drag, so the handler only records dirty objects and a 10 Hz timer sends one
coalesced patch. Membership, light, camera or world changes force ``scene/full``, since a patch
has no vocabulary for them. Handlers must be ``@persistent`` or Blender drops them on file load.
"""

from __future__ import annotations

import bpy

from .. import log
from ..authoring import entity as authoring
from ..export.entity import export_entity
from ..export.material import MaterialExporter
from ..export.mesh import MeshExporter
from ..export.placement import Placement
from ..prefs import export_paths, get_preferences
from . import protocol

__all__ = ["start", "stop"]

_session = None
_dirty_objects: set[str] = set()
_needs_full_resync = False
_known_entities: set[str] = set()
_timer_registered = False


def start(session) -> None:  # live.session.LiveSession
    """Attach the depsgraph handler and the drain timer."""
    global _session, _needs_full_resync, _known_entities

    _session = session
    _dirty_objects.clear()
    _needs_full_resync = False
    _known_entities = {obj.name for obj in authoring.entity_objects(bpy.context.scene)}

    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)

    _register_timer()


def stop() -> None:
    """Detach everything. Safe to call when nothing is attached."""
    global _session, _timer_registered

    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)

    if _timer_registered and bpy.app.timers.is_registered(_drain):
        bpy.app.timers.unregister(_drain)
    _timer_registered = False

    _dirty_objects.clear()
    _session = None


def _register_timer() -> None:
    global _timer_registered
    if not _timer_registered:
        bpy.app.timers.register(_drain, first_interval=_interval())
        _timer_registered = True


def _interval() -> float:
    try:
        return 1.0 / max(1, get_preferences().live_rate_hz)
    except (KeyError, AttributeError):
        return 0.1


@bpy.app.handlers.persistent
def _on_depsgraph_update(scene: bpy.types.Scene, depsgraph) -> None:
    """Record what changed. Deliberately cheap -- this runs on every viewport interaction."""
    global _needs_full_resync

    if _session is None:
        return

    for update in depsgraph.updates:
        datablock = update.id
        if not isinstance(datablock, bpy.types.Object):
            # Asset or scene-level data a patch cannot express.
            if isinstance(datablock, (bpy.types.Material, bpy.types.World, bpy.types.Light)):
                _needs_full_resync = True
            continue

        if authoring.is_entity(datablock):
            _dirty_objects.add(datablock.name)
        elif datablock.type in {"LIGHT", "CAMERA"}:
            # A non-entity lamp is emitted by the scene walk, not tracked as an entity, so no
            # patch can address it by name; the camera is not in the document at all.
            _needs_full_resync = True

    # Membership changes cannot be inferred from the update list alone.
    current = {obj.name for obj in authoring.entity_objects(scene)}
    if current != _known_entities:
        _needs_full_resync = True


def _drain() -> float | None:
    """Timer callback: send one coalesced update. Returns the next interval, or None to stop."""
    global _needs_full_resync, _known_entities

    if _session is None:
        return None

    if not _session.connected:
        # The runtime exited; do not spin a timer against a dead socket.
        from . import session as session_module

        log.warn("The Paradise runtime exited; stopping live preview.")
        session_module.stop()
        return None

    scene = bpy.context.scene

    try:
        if _needs_full_resync:
            _session.send_full_scene(scene)
            _known_entities = {obj.name for obj in authoring.entity_objects(scene)}
            _needs_full_resync = False
            _dirty_objects.clear()
        elif _dirty_objects:
            _send_patch(scene)
    except Exception as error:  # never let a bad frame kill the timer
        log.warn(f"Live preview update failed: {error}")
        _dirty_objects.clear()

    return _interval()


def _send_patch(scene: bpy.types.Scene) -> None:
    """Send the dirty entities as a patch. An entity that stops authoring anything forces a full
    resync: the roster is built from the ``is_entity`` FLAG, so nothing else would notice, and a
    patch says nothing about what it omits."""
    paths = export_paths(scene)

    # Fresh per patch (they cache), and with asset writing off: re-exporting a GLB mid-drag
    # would stall Blender.
    materials = MaterialExporter()
    meshes = MeshExporter()

    global _needs_full_resync

    # The same object set a full export considers, so parent links name objects the runtime has.
    exported = {obj.name for obj in authoring.entity_objects(scene)}
    exported.update(
        obj.name for obj in scene.objects
        if obj.type == "LIGHT" and not authoring.is_entity(obj)
    )
    placement = Placement(exported)

    updated = []
    for name in sorted(_dirty_objects):
        obj = scene.objects.get(name)
        if obj is None or not authoring.is_entity(obj):
            continue
        components = export_entity(obj, paths, materials, meshes)
        if components is None:
            # Exports to nothing now; see the docstring.
            _needs_full_resync = True
            continue
        # Placement adds identity and placement; the runtime keys the stream on meta.Name.
        placement.components(obj, components)
        updated.append(components.to_json())

    _dirty_objects.clear()

    if updated:
        _session.send(protocol.scene_patch(_session.next_seq(), updated=updated))
