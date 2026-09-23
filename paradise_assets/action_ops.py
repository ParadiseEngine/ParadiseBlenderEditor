"""Schema-driven C# actions and editor-owned visibility for preview providers."""

from __future__ import annotations

import contextlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, StringProperty
from bpy.types import Operator

from . import component_ops
from .document import actions, component_schema, prefab
from .materialize import action_preview, store
from .play import host

_STATE = "paradise_action_toggles"
_PREVIEW_STATE = "paradise_action_previews"
_ERROR = "paradise_action_error"
_JOBS = {}
_QUEUED = {}
_GENERATIONS = {}


def _owner(document, entity, component):
    return json.dumps([document, entity.lower(), component.lower()])


def _preview_owner(document, entity, component, action):
    return json.dumps([document, entity.lower(), component.lower(), action])


def _saved(scene, key):
    try:
        value = json.loads(scene.get(key, "{}"))
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def toggle_values(scene, entity, component):
    state = store.read_state(scene)
    if state is None:
        return {}
    values = _saved(scene, _STATE).get(_owner(state.path, entity, component), {})
    layout = store.project_of(scene)
    schema = component_schema.load(layout.root).get(component) if layout else None
    names = {action.name for action in schema.actions if action.kind == "toggle"} if schema else set()
    if not isinstance(values, dict):
        return {}
    return {key: value for key, value in values.items() if key in names and type(value) is bool}


def _set_toggles(scene, document, entity, component, values):
    saved = _saved(scene, _STATE)
    key = _owner(document, entity, component)
    previous = saved.get(key, {})
    saved[key] = {**(previous if isinstance(previous, dict) else {}), **values}
    scene[_STATE] = json.dumps(saved, sort_keys=True)


def preview_enabled(scene, entity, component, action):
    state = store.read_state(scene)
    return state is not None and _saved(scene, _PREVIEW_STATE).get(
        _preview_owner(state.path, entity, component, action)) is True


def _set_preview(scene, document, entity, component, action, enabled):
    owner = _preview_owner(document, entity, component, action)
    saved = _saved(scene, _PREVIEW_STATE)
    if enabled:
        saved[owner] = True
    else:
        saved.pop(owner, None)
    scene[_PREVIEW_STATE] = json.dumps(saved, sort_keys=True)
    key = (scene.as_pointer(), owner)
    _GENERATIONS[key] = _GENERATIONS.get(key, 0) + 1
    if not enabled:
        action_preview.clear_owner(scene, owner)
        if scene.as_pointer() in _QUEUED:
            _QUEUED[scene.as_pointer()] = [item for item in _QUEUED[scene.as_pointer()]
                                         if not item.preview or item.preview_owner != owner]
    _updated()


def busy(scene):
    return scene.as_pointer() in _JOBS


def error(scene):
    return scene.get(_ERROR, "")


def _live_owners(scene):
    state = store.read_state(scene)
    if state is None:
        return set()
    return {_owner(state.path, entity, component["id"])
            for obj in scene.collection.all_objects if (entity := store.guid_of(obj))
            for component in component_ops.components_of(obj)
            if component.get("id") and store.authors(obj, component["id"])}


def _components(scene):
    layout = store.project_of(scene)
    if layout is None:
        return
    vocabulary = component_schema.load(layout.root)
    for obj in scene.collection.all_objects:
        entity = store.guid_of(obj)
        if not entity:
            continue
        for component in component_ops.components_of(obj):
            schema = vocabulary.describe(component)
            if schema and store.authors(obj, schema.id):
                yield entity, schema


def _live_previews(scene):
    state = store.read_state(scene)
    if state is None:
        return {}
    return {_preview_owner(state.path, entity, schema.id, action.name): (entity, schema.id, action.name)
            for entity, schema in _components(scene) for action in schema.actions if action.kind == "preview"}


def prune(scene):
    state = store.read_state(scene)
    if state is None:
        return
    owners = _live_owners(scene)
    previews = _live_previews(scene)
    enabled = {key for key, value in _saved(scene, _PREVIEW_STATE).items() if value is True}
    action_preview.prune(scene, owners | (previews.keys() & enabled))
    for property_name, live in ((_STATE, owners), (_PREVIEW_STATE, previews)):
        saved = _saved(scene, property_name)
        retained = {}
        for key, value in saved.items():
            try:
                address = json.loads(key)
                other_document = isinstance(address, list) and address and address[0] != state.path
            except (ValueError, TypeError):
                other_document = False
            if other_document or key in live:
                retained[key] = value
            elif property_name == _PREVIEW_STATE:
                generation = (scene.as_pointer(), key)
                _GENERATIONS[generation] = _GENERATIONS.get(generation, 0) + 1
        if retained != saved:
            scene[property_name] = json.dumps(retained, sort_keys=True)
    key = scene.as_pointer()
    if key in _QUEUED:
        _QUEUED[key] = [item for item in _QUEUED[key] if not item.preview or
                        item.preview_owner in previews and item.preview_owner in enabled]


def _prune_pending():
    watching = False
    for scene in bpy.data.scenes:
        if scene.get(_STATE) or scene.get(_PREVIEW_STATE) or any(
                key[0] == scene.as_pointer() for key in action_preview._OVERLAYS):
            prune(scene)
            providers = _live_previews(scene)
            watching |= any(key in providers and value is True
                            for key, value in _saved(scene, _PREVIEW_STATE).items())
    # Schema rebuilds need no Blender depsgraph update, so enabled previews keep a light poll.
    return 1.0 if watching else None


@persistent
def _updated(*_):
    # Defer to the next event turn: rematerialization updates the depsgraph before its final
    # document stamp, and pruning there could erase the previous document's saved toggles.
    if not bpy.app.timers.is_registered(_prune_pending):
        bpy.app.timers.register(_prune_pending, first_interval=0.1)


def _fingerprint(scene):
    # Include live transforms, parenting, helper geometry and overlays. A pending action must
    # never rematerialize over edits made while its child process was running.
    return tuple(sorted((obj.as_pointer(), obj.name, obj.parent.as_pointer() if obj.parent else 0,
                         tuple(value for row in obj.matrix_basis for value in row),
                         tuple(value for row in obj.matrix_parent_inverse for value in row),
                         repr(sorted((key, str(value)) for key, value in obj.items())))
                        for obj in scene.collection.all_objects))


@dataclass
class _Request:
    scene: object
    document: str
    root: str
    entity: str
    component: str
    action: str
    value: bool | None = None
    on_save: bool = False
    restoring: bool = False
    preview: bool = False
    generation: int = 0
    refresh_previews: bool = True
    temporary: object = None
    job: object = None
    stamp: str = ""
    fingerprint: tuple = ()

    @property
    def directory(self):
        return Path(self.temporary.name)

    @property
    def preview_owner(self):
        return _preview_owner(self.document, self.entity, self.component, self.action)


def _preview_current(request):
    scene = request.scene
    state = store.read_state(scene)
    return (state is not None and state.path == request.document
            and preview_enabled(scene, request.entity, request.component, request.action)
            and request.preview_owner in _live_previews(scene)
            and request.generation == _GENERATIONS.get((scene.as_pointer(), request.preview_owner), 0))


def _prepare(request):
    state = store.read_state(request.scene)
    if state is None or state.path != request.document or state.is_stale:
        raise ValueError("The document changed before the authored action could start; reload it first")
    if request.preview and (state.stamp != request.stamp or _fingerprint(request.scene) != request.fingerprint):
        raise ValueError("Local edits changed before the preview could start; save to refresh it")
    cache = Path(request.root, ".editor", "actions")
    cache.mkdir(parents=True, exist_ok=True)
    request.temporary = tempfile.TemporaryDirectory(prefix="invoke-", dir=cache)
    state_path, response_path = request.directory / "state.json", request.directory / "response.json"
    state_path.write_text(json.dumps(toggle_values(request.scene, request.entity, request.component)),
                          encoding="utf-8")
    request.stamp, request.fingerprint = state.stamp, _fingerprint(request.scene)
    return actions.arguments(request.document, request.component, request.action, request.entity,
                             state_path, response_path, value=request.value, on_save=request.on_save)


def _finish(request, result):
    scene = request.scene
    state = store.read_state(scene)
    if state is None or state.path != request.document:
        return
    if request.preview and not _preview_current(request):
        prune(scene)
        return
    if result is None:
        raise ValueError("No Paradise CLI found; configure it in the addon preferences")
    if not result.ok:
        raise ValueError(result.summary())
    response = actions.response(json.loads((request.directory / "response.json").read_text(encoding="utf-8")))
    if request.preview:
        if response.document_changed or response.toggles:
            raise ValueError("A preview provider may only return viewport overlays")
        if state.is_stale or state.stamp != request.stamp or _fingerprint(scene) != request.fingerprint:
            raise ValueError("The document or local edits changed while the preview was running; "
                             "save or reload to refresh it")
        action_preview.replace(scene, request.preview_owner, response.overlays)
        return
    if response.document_changed:
        if state.stamp != request.stamp or _fingerprint(scene) != request.fingerprint:
            raise ValueError("The action updated the document; newer local edits are preserved. "
                             "Reconcile those edits before reloading the changed document")
        # Actions can change placement too: rematerialize only after the complete edit check.
        from .materialize import load
        document = prefab.loads(Path(request.document).read_text(encoding="utf-8"), request.document)
        selections = []
        for layer in scene.view_layers:
            selected = {guid for obj in layer.objects
                        if obj.select_get(view_layer=layer) and (guid := store.guid_of(obj))}
            active = layer.objects.active
            selections.append((layer, selected, store.guid_of(active) if active else None))
        with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
            load.load_document(scene, document, request.document, store.project_of(scene), preserve_actions=True)
        for layer, selected, active_guid in selections:
            for obj in layer.objects:
                if store.guid_of(obj):
                    obj.select_set(store.guid_of(obj) in selected, view_layer=layer)
            if active_guid:
                layer.objects.active = store.object_with_guid(scene, active_guid)
    elif state.is_stale:
        raise ValueError("The document changed while the authored action was running; reload it first")
    owner = _owner(request.document, request.entity, request.component)
    if owner in _live_owners(scene):
        _set_toggles(scene, request.document, request.entity, request.component, response.toggles)
        action_preview.apply(scene, owner, response.overlays)
    prune(scene)
    if response.document_changed:
        for queued in _QUEUED.get(scene.as_pointer(), []):
            if queued.preview:
                queued.stamp, queued.fingerprint = store.read_state(scene).stamp, _fingerprint(scene)
    elif _fingerprint(scene) != request.fingerprint:
        enabled = _saved(scene, _PREVIEW_STATE)
        previews = [key for key in _live_previews(scene) if enabled.get(key) is True]
        if previews:
            for preview_owner in previews:
                action_preview.clear_owner(scene, preview_owner)
            raise ValueError("Local edits changed while the action was running; save to refresh the previews")
    if request.refresh_previews:
        _refresh_previews(scene)


def _cleanup(request):
    if request.temporary is not None:
        request.temporary.cleanup()


def _record_failure(scene, exception):
    message = f"Authored action: {exception}"
    with contextlib.suppress(ReferenceError):
        scene[_ERROR] = message
    print(f"[paradise_assets] {message}")


def _failed(request):
    if request.preview:
        with contextlib.suppress(ReferenceError):
            action_preview.clear_owner(request.scene, request.preview_owner)


def _start(request):
    if request.preview and not _preview_current(request):
        prune(request.scene)
        return False
    arguments = _prepare(request)
    if bpy.app.background:
        try:
            _finish(request, host.run_cli(arguments, cwd=request.root))
        except Exception:
            _failed(request)
            raise
        finally:
            _cleanup(request)
        return True
    request.job = host.start_cli(arguments, cwd=request.root)
    if request.job is None:
        _cleanup(request)
        raise ValueError("No Paradise CLI found; configure it in the addon preferences")
    _JOBS[request.scene.as_pointer()] = request
    if not bpy.app.timers.is_registered(_poll):
        bpy.app.timers.register(_poll, first_interval=0.2)
    return True


def request(scene, entity, component, action, *, value=None, on_save=False, restoring=False,
            preview=False, refresh_previews=True):
    state, layout = store.read_state(scene), store.project_of(scene)
    if state is None or layout is None:
        raise ValueError("Open a document inside an asset project first")
    pending = _Request(scene, state.path, layout.root, entity, component, action, value, on_save, restoring,
                       preview=preview, refresh_previews=refresh_previews)
    if preview:
        pending.generation = _GENERATIONS.get((scene.as_pointer(), pending.preview_owner), 0)
        pending.stamp, pending.fingerprint = state.stamp, _fingerprint(scene)
    if busy(scene):
        queue = _QUEUED.setdefault(scene.as_pointer(), [])
        if preview:
            queue[:] = [item for item in queue if not item.preview or item.preview_owner != pending.preview_owner]
            queue.append(pending)
        else:
            # Business actions finish before providers query their resulting geometry.
            position = next((index for index, item in enumerate(queue) if item.preview), len(queue))
            queue.insert(position, pending)
    else:
        try:
            if not restoring and _ERROR in scene:
                del scene[_ERROR]
            _start(pending)
        except Exception:
            _failed(pending)
            _cleanup(pending)
            raise


def _refresh_previews(scene):
    enabled = _saved(scene, _PREVIEW_STATE)
    for owner, (entity, component, action) in _live_previews(scene).items():
        if enabled.get(owner) is not True:
            continue
        try:
            request(scene, entity, component, action, preview=True, restoring=True)
        except (OSError, ValueError, RuntimeError) as exception:
            _record_failure(scene, exception)


def _poll():
    for key, active in list(_JOBS.items()):
        result = active.job.poll()
        if result is None:
            continue
        try:
            _finish(active, result)
        except (OSError, ValueError, ReferenceError, RuntimeError) as exception:
            _failed(active)
            _record_failure(active.scene, exception)
            if not active.restoring and not active.preview:
                _QUEUED.pop(key, None)
        finally:
            del _JOBS[key]
            _cleanup(active)
        while pending := _QUEUED.get(key):
            following = pending.pop(0)
            try:
                if _start(following):
                    break
            except (OSError, ValueError, ReferenceError, RuntimeError) as exception:
                _failed(following)
                _cleanup(following)
                _record_failure(following.scene, exception)
                if not following.preview and not following.restoring:
                    _QUEUED.pop(key, None)
        if not _QUEUED.get(key):
            _QUEUED.pop(key, None)
    action_preview.redraw()
    return 0.2 if _JOBS else None


def after_save(scene):
    prune(scene)
    scheduled = []
    for entity, schema in _components(scene):
        values = toggle_values(scene, entity, schema.id)
        controls = {action.name: action.kind for action in schema.actions if action.kind != "save"}
        # A marked toggle is re-invoked with its stored value; buttons and save hooks take none.
        scheduled += [(entity, schema.id, action.name,
                       values.get(action.name, False) if controls.get(action.name) == "toggle" else None)
                      for action in schema.actions
                      if action.kind == "save" or action.on_save]
    try:
        for entity, component, action, value in scheduled:
            request(scene, entity, component, action, value=value, on_save=True,
                    refresh_previews=False)
    except (OSError, ValueError, RuntimeError) as exception:
        _record_failure(scene, exception)
        return str(exception)
    _refresh_previews(scene)
    return None


def after_load(scene):
    """Restore enabled business toggles and editor previews without invoking save hooks."""
    state = store.read_state(scene)
    if state is None:
        return
    prune(scene)
    scheduled = []
    for entity, schema in _components(scene):
        values = toggle_values(scene, entity, schema.id)
        enabled = [action.name for action in schema.actions
                   if action.kind == "toggle" and values.get(action.name, False)]
        # Do not claim a restored effect until its callback actually succeeds.
        if enabled:
            _set_toggles(scene, state.path, entity, schema.id, {name: False for name in enabled})
            scheduled.extend((entity, schema.id, name) for name in enabled)
    for entity, component, action in scheduled:
        try:
            request(scene, entity, component, action, value=True, restoring=True, refresh_previews=False)
        except (OSError, ValueError, RuntimeError) as exception:
            _record_failure(scene, exception)
    _refresh_previews(scene)


def clear(scene):
    key = scene.as_pointer()
    _QUEUED.pop(key, None)
    active = _JOBS.pop(key, None)
    if active:
        active.job.close()
        _cleanup(active)
    for address in list(_GENERATIONS):
        if address[0] == key:
            del _GENERATIONS[address]
    action_preview.clear(scene)
    if not _JOBS and bpy.app.timers.is_registered(_poll):
        bpy.app.timers.unregister(_poll)
    if _ERROR in scene:
        del scene[_ERROR]


@persistent
def _before_load(*_):
    for scene in bpy.data.scenes:
        clear(scene)
    for active in list(_JOBS.values()):
        active.job.close()
        _cleanup(active)
    _JOBS.clear()
    _QUEUED.clear()
    _GENERATIONS.clear()
    if bpy.app.timers.is_registered(_poll):
        bpy.app.timers.unregister(_poll)
    if bpy.app.timers.is_registered(_prune_pending):
        bpy.app.timers.unregister(_prune_pending)


def register_handler():
    if _before_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_before_load)
    if _updated not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_updated)


def unregister_handler():
    if _before_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_before_load)
    if _updated in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_updated)
    _before_load()


class PARADISE_ASSETS_OT_invoke_action(Operator):
    bl_idname = "paradise_assets.invoke_action"
    bl_label = "Run Authored Action"
    bl_description = "Save pending edits and invoke the component's authored C# action"

    component_id: StringProperty()
    entity_id: StringProperty()
    action_name: StringProperty()
    value: BoolProperty()

    @classmethod
    def poll(cls, context):
        return store.read_state(context.scene) is not None and not busy(context.scene)

    @classmethod
    def description(cls, context, properties):
        schema = component_ops.vocabulary_for(context).get(properties.component_id)
        action = next((item for item in schema.actions if item.name == properties.action_name), None) if schema else None
        return action.doc if action and action.doc else cls.bl_description

    def execute(self, context):
        from .materialize import save
        try:
            schema = component_ops.vocabulary_for(context).get(self.component_id)
            action = next((item for item in schema.actions
                           if item.name == self.action_name and item.kind in {"button", "toggle"}), None) if schema else None
            if action is None:
                raise ValueError("This action is no longer declared; rebuild the game schema")
            obj = store.object_with_guid(context.scene, self.entity_id)
            if obj is None or not store.authors(obj, self.component_id):
                raise ValueError("Open the source prefab to run this component's actions")
            save.save_prefab(context.scene, invoke_actions=False)
            request(context.scene, self.entity_id, self.component_id, action.name,
                    value=self.value if action.kind == "toggle" else None)
        except (OSError, ValueError, RuntimeError, save.SaveError) as exception:
            _record_failure(context.scene, exception)
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        return {"FINISHED"}


class PARADISE_ASSETS_OT_toggle_preview(Operator):
    bl_idname = "paradise_assets.toggle_preview"
    bl_label = "Show Authored Preview"
    bl_description = "Show or hide this component's viewport preview"

    component_id: StringProperty()
    entity_id: StringProperty()
    action_name: StringProperty()
    value: BoolProperty()

    @classmethod
    def poll(cls, context):
        return store.read_state(context.scene) is not None

    @classmethod
    def description(cls, context, properties):
        schema = component_ops.vocabulary_for(context).get(properties.component_id)
        action = next((item for item in schema.actions if item.name == properties.action_name), None) if schema else None
        return action.doc if action and action.doc else cls.bl_description

    def execute(self, context):
        from .materialize import save
        scene, state = context.scene, store.read_state(context.scene)
        if not self.value:
            _set_preview(scene, state.path, self.entity_id, self.component_id, self.action_name, False)
            return {"FINISHED"}
        try:
            if busy(scene):
                raise ValueError("Wait for the current authored action before enabling a preview")
            schema = component_ops.vocabulary_for(context).get(self.component_id)
            action = next((item for item in schema.actions if item.name == self.action_name
                           and item.kind == "preview"), None) if schema else None
            if action is None:
                raise ValueError("This preview is no longer declared; rebuild the game schema")
            obj = store.object_with_guid(scene, self.entity_id)
            if obj is None or not store.authors(obj, self.component_id):
                raise ValueError("Open the source prefab to show this component's preview")
            save.save_prefab(scene, invoke_actions=False)
            _set_preview(scene, state.path, self.entity_id, self.component_id, action.name, True)
            request(scene, self.entity_id, self.component_id, action.name, preview=True)
        except (OSError, ValueError, RuntimeError, save.SaveError) as exception:
            _record_failure(scene, exception)
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        return {"FINISHED"}


def draw(layout, context, obj, component, schema):
    if not schema.actions:
        return
    scene, entity = context.scene, store.guid_of(obj)
    values = toggle_values(scene, entity, component)
    controls = layout.column(align=True)
    owned = store.authors(obj, component)
    for action in schema.actions:
        if action.kind == "save":
            continue
        preview = action.kind == "preview"
        enabled = preview_enabled(scene, entity, component, action.name) if preview else values.get(action.name, False)
        row = controls.row(align=True)
        row.enabled = owned and (not busy(scene) or preview and enabled)
        options = {"text": action.display_name}
        if preview:
            options.update(icon="HIDE_OFF" if enabled else "HIDE_ON", depress=enabled)
        elif action.kind == "toggle":
            options.update(icon="CHECKBOX_HLT" if enabled else "CHECKBOX_DEHLT", depress=enabled)
        operator = row.operator("paradise_assets.toggle_preview" if preview else "paradise_assets.invoke_action",
                                **options)
        operator.component_id, operator.entity_id = component, entity
        operator.action_name, operator.value = action.name, not enabled
    if not owned:
        layout.label(text="Open the source prefab to run its actions", icon="LOCKED")
    if busy(scene):
        layout.label(text="Running authored action...", icon="TIME")
    if error(scene):
        layout.label(text=error(scene), icon="ERROR")


classes = (PARADISE_ASSETS_OT_invoke_action, PARADISE_ASSETS_OT_toggle_preview)
