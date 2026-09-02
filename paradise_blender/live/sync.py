"""Turning Blender edits into live-preview messages.

The core problem is rate. ``depsgraph_update_post`` fires on essentially every mouse-move
during a drag -- hundreds of times per second -- and each firing may report the same object
repeatedly. Sending a message per firing would saturate the socket and stall the runtime
while conveying no more information than a tenth of them would.

So the handler does almost nothing: it records *which* objects are dirty in a set and returns.
A timer drains that set at the configured rate (default 10 Hz) and sends one coalesced patch.
Dragging an object across the viewport for a second produces ~10 messages instead of ~500,
and the last one always carries the final position.

Choosing between a patch and a full resync:

* an entity's **transform or properties** changed -> ``scene/patch`` with just that entity
* an entity was **added or removed**, or the light/camera/world changed -> ``scene/full``,
  because a patch has no vocabulary for those and a partial update would leave the runtime's
  scene silently out of step

Handlers must be ``@persistent`` or Blender drops them when a file is loaded -- and a preview
that stops updating after the user opens another .blend, with no error, is a bad failure.
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
            # A material, world, light data, or mesh datablock changed. These affect assets or
            # scene-level data that a patch cannot express, so resync wholesale.
            if isinstance(datablock, (bpy.types.Material, bpy.types.World, bpy.types.Light)):
                _needs_full_resync = True
            continue

        if authoring.is_entity(datablock):
            _dirty_objects.add(datablock.name)
        elif datablock.type in {"LIGHT", "CAMERA"}:
            # Lights and the camera live outside the entity list, in the level document's
            # header -- there is no patch message for them.
            _needs_full_resync = True

    # Membership changes (an entity added, removed, or its flag toggled) cannot be inferred
    # from the update list alone, so compare the roster.
    current = {obj.name for obj in authoring.entity_objects(scene)}
    if current != _known_entities:
        _needs_full_resync = True


def _drain() -> float | None:
    """Timer callback: send one coalesced update. Returns the next interval, or None to stop."""
    global _needs_full_resync, _known_entities

    if _session is None:
        return None

    if not _session.connected:
        # The runtime exited. Tear the session down rather than spinning a timer against a
        # dead socket.
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
    """Send the dirty entities as a patch.

    Rebuilds each dirty entity's full document rather than diffing fields -- see
    :func:`..live.protocol.scene_patch` for why whole entities are the unit of change.

    AN ENTITY THAT STOPS AUTHORING ANYTHING forces a full resync. ``export_entity`` returns None
    for an object that says nothing beyond its name and placement, and such an object is no longer
    in the document -- but nothing else here would notice: the roster comparison in
    :func:`_on_depsgraph_update` is built from the ``is_entity`` FLAG, which does not change when
    the last authored component is removed. Without this the object would simply be missing from
    the patch, and a patch says nothing about what it omits, so the runtime would keep drawing a
    thing the scene no longer describes until an unrelated structural change or a restart.
    """
    paths = export_paths(scene)

    # Fresh collaborators per patch: these cache by design, and a stale cache would make an
    # edited material or mesh look unchanged. Asset *writing* is suppressed -- the exporters
    # here only resolve references, since re-exporting a GLB mid-drag would stall Blender.
    materials = MaterialExporter()
    meshes = MeshExporter()

    global _needs_full_resync

    # Parent links in a patch have to name objects the runtime already has. The set is the
    # entity roster plus non-entity lamps -- the same objects a full export would consider --
    # so a child moved under an empty that is not exported still hangs from the next ancestor
    # that is.
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
            # It exports to nothing now. A patch has no way to say "this one is gone" about an
            # object it never names, so the whole scene goes again -- see the docstring.
            _needs_full_resync = True
            continue
        # export_entity no longer writes identity or placement: those need the whole exported
        # set, which Placement has. A patch without them is a nameless, unplaced object, and
        # the mock (and a real runtime) keys the stream on meta.Name.
        placement.components(obj, components)
        updated.append(components.to_json())

    _dirty_objects.clear()

    if updated:
        _session.send(protocol.scene_patch(_session.next_seq(), updated=updated))
