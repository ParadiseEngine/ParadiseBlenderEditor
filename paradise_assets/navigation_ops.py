"""Scene navigation baking and transient preview, using the engine's Recast/Detour CLI."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.types import Operator

from . import component_ops
from .document import component_schema, navigation
from .materialize import navigation_geometry, navigation_preview, store
from .play import host

_AUTO = "paradise_navigation_auto_bake"
_SETTINGS_PATH = "paradise_navigation_settings_path"
_ERROR = "paradise_navigation_error"
_JOBS = {}
_QUEUED = {}


def auto_bake(scene):
    state = store.read_state(scene)
    return bool(state and scene.get(_SETTINGS_PATH) == state.path and scene.get(_AUTO, False))


def error(scene):
    return scene.get(_ERROR, "")


def busy(scene):
    return scene.as_pointer() in _JOBS


def path_for(scene):
    state, layout = store.read_state(scene), store.project_of(scene)
    if state is None or layout is None:
        raise ValueError("Open a level document inside an asset project first")
    return navigation.asset_path(state.path, layout.assets)


def _has_navigation(scene):
    layout = store.project_of(scene)
    if layout is None:
        return False
    vocabulary = component_schema.load(layout.root)
    for obj in scene.collection.all_objects:
        if not store.guid_of(obj):
            continue
        for component in component_ops.components_of(obj):
            schema = vocabulary.describe(component)
            if schema and navigation.fields(schema, component.get("data", {})):
                return True
    return False


@dataclass
class _Request:
    scene: object
    document: str
    root: str
    output: Path
    temporary: object
    arguments: list[str]
    bake: bool
    stamp: str
    job: object = None

    @property
    def directory(self):
        return Path(self.temporary.name)


def _prepare(scene, bake):
    relative = path_for(scene)
    layout = store.project_of(scene)
    output = Path(layout.assets, relative)
    if not _has_navigation(scene):
        raise ValueError("Add a Scene navigation component before baking or previewing")
    if not bake and not output.is_file():
        raise ValueError("No baked navigation mesh exists yet; use Bake first")
    geometry = navigation_geometry.snapshot(scene) if bake else None
    cache = Path(layout.editor, "navigation")
    cache.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="bake-" if bake else "preview-", dir=cache)
    directory = Path(temporary.name)
    try:
        if bake:
            (directory / "geometry.json").write_text(json.dumps(geometry), encoding="utf-8")
            arguments = ["assets", "bake-navmesh", "--input", str(directory / "geometry.json"),
                         "--output", str(directory / "level.navmesh"),
                         "--preview", str(directory / "preview.json")]
        else:
            arguments = ["assets", "preview-navmesh", "--input", str(output),
                         "--output", str(directory / "preview.json")]
        state = store.read_state(scene)
        return _Request(scene, state.path, layout.root, output,
                        temporary, arguments, bake, state.stamp)
    except Exception:
        temporary.cleanup()
        raise


def _finish(request, result):
    scene = request.scene
    state = store.read_state(scene)
    if state is None or state.path != request.document:
        return
    if request.bake and (state.stamp != request.stamp or state.is_stale):
        return
    if result is None:
        raise ValueError("No Paradise CLI found; configure it in the addon preferences")
    if not result.ok:
        raise ValueError(result.summary())
    payload = json.loads((request.directory / "preview.json").read_text(encoding="utf-8"))
    navigation_preview.geometry(payload)
    if request.bake:
        # Promote only a complete bake. The watcher never sees the CLI's temporary output.
        os.replace(request.directory / "level.navmesh", request.output)
    if not request.bake or navigation_preview.is_visible(scene):
        navigation_preview.show(scene, payload)
    if _ERROR in scene:
        del scene[_ERROR]


def _record_failure(scene, exception):
    message = f"Navigation: {exception}"
    try:
        scene[_ERROR] = message
    except ReferenceError:
        pass
    print(f"[paradise_assets] {message}")


def _start(request):
    scene = request.scene
    key = scene.as_pointer()
    if bpy.app.background:
        try:
            _finish(request, host.run_cli(request.arguments, cwd=request.root))
        finally:
            request.temporary.cleanup()
        return
    request.job = host.start_cli(request.arguments, cwd=request.root)
    if request.job is None:
        request.temporary.cleanup()
        raise ValueError("No Paradise CLI found; configure it in the addon preferences")
    _JOBS[key] = request
    if not bpy.app.timers.is_registered(_poll):
        bpy.app.timers.register(_poll, first_interval=0.2)


def request(scene, *, bake):
    prepared = _prepare(scene, bake)
    key = scene.as_pointer()
    if key in _JOBS:
        previous = _QUEUED.pop(key, None)
        if previous:
            previous.temporary.cleanup()
        _QUEUED[key] = prepared
    else:
        try:
            _start(prepared)
        except Exception:
            prepared.temporary.cleanup()
            raise


def _poll():
    for key, active in list(_JOBS.items()):
        result = active.job.poll()
        if result is None:
            continue
        del _JOBS[key]
        pending = _QUEUED.pop(key, None)
        try:
            # A newer save supersedes the in-flight snapshot, including its preview.
            if pending is None:
                _finish(active, result)
        except (OSError, ValueError, ReferenceError, RuntimeError) as exception:
            _record_failure(active.scene, exception)
        finally:
            active.temporary.cleanup()
        if pending is not None:
            try:
                _start(pending)
            except (OSError, ValueError, ReferenceError, RuntimeError) as exception:
                if pending.job is not None:
                    pending.job.close()
                pending.temporary.cleanup()
                _record_failure(pending.scene, exception)
    navigation_preview._redraw()
    return 0.2 if _JOBS else None


def after_save(scene):
    if auto_bake(scene) and _has_navigation(scene):
        try:
            request(scene, bake=True)
        except (OSError, ValueError, RuntimeError) as exception:
            _record_failure(scene, exception)
            return str(exception)
    return None


def _clear_job(key):
    pending = _QUEUED.pop(key, None)
    if pending:
        pending.temporary.cleanup()
    active = _JOBS.pop(key, None)
    if active:
        active.job.close()
        active.temporary.cleanup()
    if not _JOBS and bpy.app.timers.is_registered(_poll):
        bpy.app.timers.unregister(_poll)


def clear(scene):
    _clear_job(scene.as_pointer())
    navigation_preview.clear(scene)
    if _ERROR in scene:
        del scene[_ERROR]


@persistent
def _before_load(*_):
    # A scene may have been deleted while its child process was still running.
    for key in set(_JOBS) | set(_QUEUED):
        _clear_job(key)
    for scene in bpy.data.scenes:
        clear(scene)


def register_handler():
    if _before_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_before_load)


def unregister_handler():
    if _before_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_before_load)
    _before_load()


class PARADISE_ASSETS_OT_bake_navigation(Operator):
    bl_idname = "paradise_assets.bake_navigation"
    bl_label = "Bake"
    bl_description = "Save this level and bake navigation from its static mesh geometry"

    def execute(self, context):
        from .materialize import save
        try:
            if not _has_navigation(context.scene):
                raise ValueError("Add a Scene navigation component first")
            save.save_prefab(context.scene, bake_navigation=False)
            request(context.scene, bake=True)
        except (OSError, ValueError, RuntimeError, save.SaveError) as exception:
            _record_failure(context.scene, exception)
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        self.report({"INFO"}, "Baking navigation" if busy(context.scene) else "Navigation baked")
        return {"FINISHED"}


class PARADISE_ASSETS_OT_toggle_navigation_preview(Operator):
    bl_idname = "paradise_assets.toggle_navigation_preview"
    bl_label = "Navigation Preview"
    bl_description = "Show or hide the baked walkable surface in the viewport"

    def execute(self, context):
        if busy(context.scene):
            return {"CANCELLED"}
        if navigation_preview.is_visible(context.scene):
            navigation_preview.hide(context.scene)
            return {"FINISHED"}
        try:
            request(context.scene, bake=False)
        except (OSError, ValueError, RuntimeError) as exception:
            _record_failure(context.scene, exception)
            self.report({"ERROR"}, str(exception))
            return {"CANCELLED"}
        return {"FINISHED"}


class PARADISE_ASSETS_OT_toggle_navigation_auto_bake(Operator):
    bl_idname = "paradise_assets.toggle_navigation_auto_bake"
    bl_label = "Auto-bake on Save"
    bl_description = "Bake navigation after Save or Ctrl+S; stored in this level's working blend"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        state = store.read_state(scene)
        if state is None:
            return {"CANCELLED"}
        enabled = not auto_bake(scene)
        scene[_SETTINGS_PATH], scene[_AUTO] = state.path, enabled
        navigation_preview._redraw()
        return {"FINISHED"}


def draw(layout, context, field_path):
    scene = context.scene
    try:
        path = path_for(scene)
    except ValueError as exception:
        layout.label(text=str(exception), icon="ERROR")
        return
    layout.label(text=f"{field_path}: {path}", icon="LOCKED")
    row = layout.row(align=True)
    row.enabled = not busy(scene)
    row.operator("paradise_assets.bake_navigation", text="Bake", icon="MOD_BUILD")
    shown = navigation_preview.is_visible(scene)
    row.operator("paradise_assets.toggle_navigation_preview", text="Preview",
                 icon="CHECKBOX_HLT" if shown else "CHECKBOX_DEHLT", depress=shown)
    enabled = auto_bake(scene)
    layout.operator("paradise_assets.toggle_navigation_auto_bake", text="Auto-bake on Save",
                    icon="CHECKBOX_HLT" if enabled else "CHECKBOX_DEHLT", depress=enabled)
    if busy(scene):
        layout.label(text="Updating navigation...", icon="TIME")
    if error(scene):
        layout.label(text=error(scene), icon="ERROR")


classes = (PARADISE_ASSETS_OT_bake_navigation, PARADISE_ASSETS_OT_toggle_navigation_preview,
           PARADISE_ASSETS_OT_toggle_navigation_auto_bake)
