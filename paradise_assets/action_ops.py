"""Schema-driven authored actions. C# owns every effect and returned value."""

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
_ERROR = "paradise_action_error"
_JOBS = {}
_QUEUED = {}


def _owner(document, entity, component):
    return json.dumps([document, entity.lower(), component.lower()])


def toggle_values(scene, entity, component):
    state = store.read_state(scene)
    if state is None:
        return {}
    try:
        saved = json.loads(scene.get(_STATE, "{}"))
        values = saved.get(_owner(state.path, entity, component), {})
        return {key: value for key, value in values.items() if type(value) is bool}
    except (ValueError, AttributeError, TypeError):
        return {}


def _set_toggles(scene, document, entity, component, values):
    try:
        saved = json.loads(scene.get(_STATE, "{}"))
        if not isinstance(saved, dict):
            saved = {}
    except (ValueError, TypeError):
        saved = {}
    key = _owner(document, entity, component)
    previous = saved.get(key, {})
    saved[key] = {**(previous if isinstance(previous, dict) else {}), **values}
    scene[_STATE] = json.dumps(saved, sort_keys=True)


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


def prune(scene):
    state = store.read_state(scene)
    if state is None:
        return
    owners = _live_owners(scene)
    action_preview.prune(scene, owners)
    try:
        saved = json.loads(scene.get(_STATE, "{}"))
        if not isinstance(saved, dict):
            return
        retained = {}
        for key, value in saved.items():
            address = json.loads(key)
            if address[0] != state.path or key in owners:
                retained[key] = value
        if retained != saved:
            scene[_STATE] = json.dumps(retained, sort_keys=True)
    except (ValueError, TypeError, IndexError):
        return


def _prune_pending():
    for scene in bpy.data.scenes:
        if scene.get(_STATE) or any(key[0] == scene.as_pointer() for key in action_preview._OVERLAYS):
            prune(scene)
    return None


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
    temporary: object = None
    job: object = None
    stamp: str = ""
    fingerprint: tuple = ()

    @property
    def directory(self):
        return Path(self.temporary.name)


def _prepare(request):
    state = store.read_state(request.scene)
    if state is None or state.path != request.document or state.is_stale:
        raise ValueError("The document changed before the authored action could start; reload it first")
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
    if result is None:
        raise ValueError("No Paradise CLI found; configure it in the addon preferences")
    if not result.ok:
        raise ValueError(result.summary())
    response = actions.response(json.loads((request.directory / "response.json").read_text(encoding="utf-8")))
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


def _cleanup(request):
    if request.temporary is not None:
        request.temporary.cleanup()


def _record_failure(scene, exception):
    message = f"Authored action: {exception}"
    with contextlib.suppress(ReferenceError):
        scene[_ERROR] = message
    print(f"[paradise_assets] {message}")


def _start(request):
    arguments = _prepare(request)
    if bpy.app.background:
        try:
            _finish(request, host.run_cli(arguments, cwd=request.root))
        finally:
            _cleanup(request)
        return
    request.job = host.start_cli(arguments, cwd=request.root)
    if request.job is None:
        _cleanup(request)
        raise ValueError("No Paradise CLI found; configure it in the addon preferences")
    _JOBS[request.scene.as_pointer()] = request
    if not bpy.app.timers.is_registered(_poll):
        bpy.app.timers.register(_poll, first_interval=0.2)


def request(scene, entity, component, action, *, value=None, on_save=False, restoring=False):
    state, layout = store.read_state(scene), store.project_of(scene)
    if state is None or layout is None:
        raise ValueError("Open a document inside an asset project first")
    pending = _Request(scene, state.path, layout.root, entity, component, action, value, on_save, restoring)
    if busy(scene):
        _QUEUED.setdefault(scene.as_pointer(), []).append(pending)
    else:
        try:
            if not restoring and _ERROR in scene:
                del scene[_ERROR]
            _start(pending)
        except Exception:
            _cleanup(pending)
            raise


def _poll():
    for key, active in list(_JOBS.items()):
        result = active.job.poll()
        if result is None:
            continue
        del _JOBS[key]
        pending = _QUEUED.pop(key, [])
        try:
            _finish(active, result)
        except (OSError, ValueError, ReferenceError, RuntimeError) as exception:
            _record_failure(active.scene, exception)
            if not active.restoring:
                pending = []
        finally:
            _cleanup(active)
        if pending:
            _QUEUED[key] = pending[1:]
            try:
                _start(pending[0])
            except (OSError, ValueError, ReferenceError, RuntimeError) as exception:
                _cleanup(pending[0])
                _QUEUED.pop(key, None)
                _record_failure(pending[0].scene, exception)
    action_preview.redraw()
    return 0.2 if _JOBS else None


def after_save(scene):
    prune(scene)
    layout = store.project_of(scene)
    if layout is None:
        return None
    vocabulary = component_schema.load(layout.root)
    scheduled = []
    for obj in scene.collection.all_objects:
        entity = store.guid_of(obj)
        if not entity or store.local_of(obj):
            continue
        for component in component_ops.components_of(obj):
            schema = vocabulary.describe(component)
            if schema and store.authors(obj, schema.id):
                scheduled.extend((entity, schema.id, action.name) for action in schema.actions if action.on_save)
    try:
        for entity, component, action in scheduled:
            request(scene, entity, component, action, on_save=True)
    except (OSError, ValueError, RuntimeError) as exception:
        _record_failure(scene, exception)
        return str(exception)
    return None


def after_load(scene):
    """Restore enabled effects by invoking their own C# toggle callbacks, never save hooks."""
    layout, state = store.project_of(scene), store.read_state(scene)
    if layout is None or state is None:
        return
    vocabulary = component_schema.load(layout.root)
    scheduled = []
    for obj in scene.collection.all_objects:
        entity = store.guid_of(obj)
        if not entity:
            continue
        for component in component_ops.components_of(obj):
            schema = vocabulary.describe(component)
            if schema is None or not store.authors(obj, schema.id):
                continue
            values = toggle_values(scene, entity, schema.id)
            enabled = [action.name for action in schema.actions
                       if action.kind == "toggle" and values.get(action.name, False)]
            # Do not claim a restored effect until its callback actually succeeds.
            if enabled:
                _set_toggles(scene, state.path, entity, schema.id, {name: False for name in enabled})
                scheduled.extend((entity, schema.id, name) for name in enabled)
    for entity, component, action in scheduled:
        try:
            request(scene, entity, component, action, value=True, restoring=True)
        except (OSError, ValueError, RuntimeError) as exception:
            _record_failure(scene, exception)


def clear(scene):
    key = scene.as_pointer()
    _QUEUED.pop(key, None)
    active = _JOBS.pop(key, None)
    if active:
        active.job.close()
        _cleanup(active)
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
            action = next((item for item in schema.actions if item.name == self.action_name), None) if schema else None
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


def draw(layout, context, obj, component, schema):
    if not schema.actions:
        return
    scene, entity = context.scene, store.guid_of(obj)
    values = toggle_values(scene, entity, component)
    controls = layout.column(align=True)
    owned = store.authors(obj, component)
    controls.enabled = not busy(scene) and owned
    for action in schema.actions:
        enabled = values.get(action.name, False)
        options = {"text": action.display_name}
        if action.kind == "toggle":
            options.update(icon="CHECKBOX_HLT" if enabled else "CHECKBOX_DEHLT", depress=enabled)
        operator = controls.operator("paradise_assets.invoke_action", **options)
        operator.component_id, operator.entity_id = component, entity
        operator.action_name, operator.value = action.name, not enabled
    if not owned:
        layout.label(text="Open the source prefab to run its actions", icon="LOCKED")
    if busy(scene):
        layout.label(text="Running authored action...", icon="TIME")
    if error(scene):
        layout.label(text=error(scene), icon="ERROR")


classes = (PARADISE_ASSETS_OT_invoke_action,)
