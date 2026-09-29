"""Refresh open document views after their recorded inputs settle, without overwriting edits."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import bpy
from bpy.app.handlers import persistent

from . import store

_INTERVAL = 0.5
_QUIET = 0.3


@dataclass
class _Session:
    document: str
    sources: dict[str, str]
    fingerprint: tuple
    pending: dict[str, str] | None = None
    changed_at: float = 0.0
    failed: bool = False
    reason: str | None = None
    saved_stamp: str | None = None
    scene_name: str = ""


_SESSIONS: dict[int, _Session] = {}


def loaded(scene: bpy.types.Scene, sources: dict[str, str]) -> None:
    """Record the files and editable object state that this materialization actually used."""
    state = store.read_state(scene)
    if state is None:
        return
    stamps = {os.path.normcase(os.path.abspath(path)): stamp for path, stamp in sources.items()}
    stamps.setdefault(os.path.normcase(state.path), state.stamp)
    _SESSIONS[scene.as_pointer()] = _Session(
        state.path, stamps, store.scene_fingerprint(scene, document_only=True), scene_name=scene.name)


def deferred(scene: bpy.types.Scene) -> None:
    """Keep watching a workfile whose initial refresh was refused until it is safe to read."""
    state = store.read_state(scene)
    if state is not None and scene.as_pointer() not in _SESSIONS:
        loaded(scene, {state.path: ""})


def saved(scene: bpy.types.Scene) -> None:
    """Accept saved local edits without marking outstanding dependency changes as displayed."""
    deferred(scene)
    session = _SESSIONS.get(scene.as_pointer())
    if session is not None:
        session.fingerprint = store.scene_fingerprint(scene, document_only=True)
        session.saved_stamp = store.read_state(scene).stamp
        # The changed document will reload once, picking up newly placed prefab dependencies.
        # Advancing every input stamp here would silently swallow an external prefab edit.


def pending_reason(scene: bpy.types.Scene) -> str | None:
    """Cached refresh status for the sidebar; drawing never discovers dependencies."""
    session = _SESSIONS.get(scene.as_pointer())
    return session.reason if session is not None else None


def _reason(session: _Session, message: str | None) -> None:
    if session.reason == message:
        return
    session.reason = message
    _redraw()


def _redraw() -> None:
    for window in bpy.context.window_manager.windows:
        if window.screen is None:
            continue
        for area in window.screen.areas:
            if area.type in {"VIEW_3D", "OUTLINER"}:
                area.tag_redraw()


def _blocked(scene: bpy.types.Scene, session: _Session) -> str | None:
    from .. import action_ops
    from . import workfile

    manager = bpy.context.window_manager
    if manager.is_interface_locked or bpy.app.is_job_running("RENDER"):
        return "Blender is busy; waiting to reload."
    if any(window.scene == scene and window.modal_operators for window in manager.windows):
        return "Finish the current operation to reload."
    if any(obj.mode != "OBJECT" for obj in scene.objects):
        return "Return to Object Mode to reload."
    if action_ops.busy(scene):
        return "Waiting for the authored action to finish."
    if (unsaved := workfile.unsaved_work(scene)) is not None:
        return unsaved
    if store.scene_fingerprint(scene, document_only=True) != session.fingerprint:
        return "Unsaved object, placement, or hierarchy edits."
    return None


def _selection_key(obj) -> tuple[str, str] | None:
    if obj is None:
        return None
    guid = store.guid_of(obj)
    return ("guid", guid) if guid else ("name", obj.name)


def _reload(scene: bpy.types.Scene, session: _Session, current: dict[str, str]) -> str | None:
    from ..document.prefab import loads
    from . import light_preview, load, shapes, transform_helpers

    layout = store.project_of(scene)
    if layout is None:
        return f"No asset project for {session.document}"
    with open(session.document, encoding="utf-8") as handle:
        document = loads(handle.read(), session.document)
    document_key = os.path.normcase(os.path.abspath(session.document))
    preserve_actions = (current.get(document_key) == session.saved_stamp
                        and all(path == document_key or stamp == session.sources[path]
                                for path, stamp in current.items()))

    def rebuilt(obj) -> bool:
        return bool(store.guid_of(obj) or shapes.is_shape(obj) or transform_helpers.is_helper(obj)
                    or light_preview.is_preview(obj))

    # The load deletes document objects and remakes them with the shapes, helpers and light
    # previews it hangs under them; Blender unparents (and so moves) the author's extras from a
    # deleted parent. Re-attach each extra to its parent's replacement by identity.
    extras = [(obj, _selection_key(obj.parent), obj.matrix_parent_inverse.copy(),
               obj.matrix_basis.copy(), obj.matrix_world.copy())
              for obj in scene.objects
              if obj.parent is not None and rebuilt(obj.parent) and not rebuilt(obj)]

    selections = []
    for layer in scene.view_layers:
        selected = {_selection_key(obj) for obj in layer.objects if obj.select_get(view_layer=layer)}
        selections.append((layer, selected, _selection_key(layer.objects.active)))
    try:
        with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
            load.load_document(scene, document, session.document, layout, require_complete=True,
                               preserve_actions=preserve_actions,
                               document_stamp=current[document_key])
    except load.LoadError as error:
        for path in error.sources:
            session.sources.setdefault(os.path.normcase(os.path.abspath(path)), store.stamp_of(path))
        return str(error)
    finally:
        parents = {_selection_key(obj): obj for obj in scene.objects}
        for obj, key, inverse, basis, world in extras:
            obj.parent = parents.get(key)
            obj.matrix_parent_inverse = inverse
            obj.matrix_basis = basis
            if obj.parent is None:
                obj.matrix_world = world
        # Imports change selection even for extras that survive rematerialization. Restore each
        # view layer by document identity (or by name for surviving extras and helper handles).
        for layer, selected, active in selections:
            for obj in layer.objects:
                key = _selection_key(obj)
                obj.select_set(key in selected, view_layer=layer)
                if active is not None and key == active:
                    layer.objects.active = obj


def tick(*, now: float | None = None) -> float:
    """Poll only files read by open levels; Blender invokes this on its main thread."""
    if not hasattr(bpy.data, "scenes"):
        return _INTERVAL
    now = time.monotonic() if now is None else now
    scenes = {scene.as_pointer(): scene for scene in bpy.data.scenes}
    for key, session in list(_SESSIONS.items()):
        try:
            scene = scenes.get(key)
            state = store.read_state(scene) if scene is not None else None
            if state is None or state.path != session.document:
                _SESSIONS.pop(key, None)
                continue
            session.scene_name = scene.name
            current = {path: store.stamp_of(path) for path in session.sources}
            if current == session.sources:
                session.pending = None
                session.failed = False
                _reason(session, None)
                continue
            if current != session.pending:
                session.pending = current
                session.changed_at = now
                session.failed = False
                _reason(session, None)
                continue
            if now - session.changed_at < _QUIET or session.failed:
                continue
            if (blocked := _blocked(scene, session)) is not None:
                _reason(session, blocked)
                continue
            problem = _reload(scene, session, current)
        except Exception as error:
            # Blender unregisters a timer that raises; one bad input must not stop every level.
            problem = str(error)
        # A load that completed installed a new session; a later failure belongs to that one.
        session = _SESSIONS.get(key, session)
        if problem is not None:
            session.failed = True
            _reason(session, problem)
            print(f"[paradise_assets] automatic reload deferred: {problem}")
        else:
            _redraw()
    return _INTERVAL


@persistent
def _before_load(*_args) -> None:
    _SESSIONS.clear()


@persistent
def _after_undo(*_args) -> None:
    # Undo restores old model caches too. Invalidate the view, but retain the last accepted
    # fingerprint: treating the undone placement as a fresh baseline would discard the undo.
    previous = {(session.document, session.scene_name): session for session in _SESSIONS.values()}
    _SESSIONS.clear()
    for scene in bpy.data.scenes:
        state = store.read_state(scene)
        if state is None:
            continue
        session = previous.get((state.path, scene.name))
        if session is None:
            session = _Session(state.path, {}, (None,), scene_name=scene.name)
        session.sources[os.path.normcase(os.path.abspath(state.path))] = ""
        session.pending = None
        session.saved_stamp = None
        session.failed = False
        _SESSIONS[scene.as_pointer()] = session
        _reason(session, "Undo/redo changed the working view; save or Reload to refresh.")


def register_handler() -> None:
    if _before_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_before_load)
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _after_undo not in handlers:
            handlers.append(_after_undo)
    if not bpy.app.timers.is_registered(tick):
        bpy.app.timers.register(tick, first_interval=_INTERVAL, persistent=True)


def unregister_handler() -> None:
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    if _before_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_before_load)
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _after_undo in handlers:
            handlers.remove(_after_undo)
    _SESSIONS.clear()
