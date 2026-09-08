"""Native lamps project canonical components. They carry no document identity and never save."""

from __future__ import annotations

import copy
import math

import bpy
from bpy.app.handlers import persistent
from mathutils import Matrix, Vector

from .. import edits
from ..document import component_schema
from ..document.light_preview import describe
from . import store

_MARKER = "paradise_light_preview"
_ERROR = "paradise_light_preview_error"
_REFRESHING = False
_TYPES = {"Directional": "SUN", "Point": "POINT", "Spot": "SPOT"}


def is_preview(obj):
    return bool(obj.get(_MARKER))


def _remove(obj):
    data = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if isinstance(data, bpy.types.Light) and data.users == 0:
        bpy.data.lights.remove(data)


def clear(scene):
    for obj in list(scene.collection.all_objects):
        if is_preview(obj):
            _remove(obj)


def refresh(scene):
    global _REFRESHING
    if _REFRESHING:
        return
    layout = store.project_of(scene)
    if layout is None:
        return
    vocabulary = component_schema.load(layout.root)
    _REFRESHING = True
    try:
        children = {}
        for helper in list(scene.collection.all_objects):
            if not is_preview(helper):
                continue
            owner = helper.parent
            if owner is None or not store.guid_of(owner) or owner in children:
                _remove(helper)
            else:
                children[owner] = helper
        owners = [obj for obj in scene.collection.all_objects if store.guid_of(obj)]
        for obj in owners:
            components = copy.deepcopy(
                edits.visible_components(store.component_json(obj), edits.read_structure(obj))
            )
            for component in components:
                data = component.setdefault("data", {})
                for path, value in edits.edited_fields(obj, component.get("id", "")).items():
                    edits.write_path(data, path, copy.deepcopy(value))
            try:
                plan = describe(components, vocabulary)
                if _ERROR in obj:
                    del obj[_ERROR]
            except ValueError as error:
                message = str(error)
                if obj.get(_ERROR) != message:
                    obj[_ERROR] = message
                    print(f"Paradise light preview: {obj.name}: {message}")
                plan = None
            helper = children.get(obj)
            if plan is None:
                if helper is not None:
                    _remove(helper)
                continue
            if helper is None:
                data = bpy.data.lights.new(f"{obj.name} preview", type=_TYPES[plan.kind])
                helper = bpy.data.objects.new(f"{obj.name} light preview", data)
                scene.collection.objects.link(helper)
                helper[_MARKER] = True
                helper.parent = obj
                helper.matrix_parent_inverse.identity()
                helper.hide_select = True
            _apply(helper, obj, plan)
    finally:
        _REFRESHING = False


def _set(data, field, value):
    if isinstance(value, float):
        prop = data.bl_rna.properties[field]
        value = max(prop.hard_min, min(prop.hard_max, value))
    current = getattr(data, field)
    if isinstance(value, tuple):
        if all(abs(a - b) < 1e-6 for a, b in zip(current, value, strict=True)):
            return
    elif isinstance(value, float):
        if math.isclose(current, value, rel_tol=1e-6, abs_tol=1e-6):
            return
    elif current == value:
        return
    setattr(data, field, value)


def _apply(helper, owner, plan):
    data = helper.data
    _set(data, "type", _TYPES[plan.kind])
    _set(data, "color", plan.color)
    _set(data, "energy", plan.energy)
    _set(data, "use_shadow", plan.shadows)
    if plan.kind != "Directional":
        _set(data, "shadow_soft_size", plan.radius)
        _set(data, "use_custom_distance", plan.range > 0.0)
        if plan.range > 0.0:
            _set(data, "cutoff_distance", plan.range)
    if plan.kind == "Spot":
        _set(data, "spot_size", plan.outer)
        _set(data, "spot_blend", plan.blend)

    # Document -Z maps to Blender +Y. Use the transformed axis itself rather than a
    # quaternion decomposition: a nonuniformly scaled parent can shear a lamp's local frame.
    if plan.kind == "Directional":
        x, y, z = plan.direction
        travel = Vector((x, -z, y))
    else:
        travel = owner.matrix_world.to_3x3() @ Vector((0.0, 1.0, 0.0))
    if travel.length_squared < 1e-12:
        travel = Vector((0.0, 0.0, -1.0))
    rotation = travel.to_track_quat("-Z", "Y")
    world = Matrix.LocRotScale(owner.matrix_world.translation, rotation, Vector((1.0, 1.0, 1.0)))
    if any(abs(helper.matrix_world[r][c] - world[r][c]) > 1e-6 for r in range(4) for c in range(4)):
        helper.matrix_world = world


@persistent
def _changed(scene, *_):
    refresh(scene)


@persistent
def _loaded(*_):
    for scene in bpy.data.scenes:
        refresh(scene)


def register_handler():
    for handlers, callback in (
        (bpy.app.handlers.depsgraph_update_post, _changed),
        (bpy.app.handlers.undo_post, _loaded),
        (bpy.app.handlers.redo_post, _loaded),
        (bpy.app.handlers.load_post, _loaded),
    ):
        if callback not in handlers:
            handlers.append(callback)


def unregister_handler():
    for handlers, callback in (
        (bpy.app.handlers.depsgraph_update_post, _changed),
        (bpy.app.handlers.undo_post, _loaded),
        (bpy.app.handlers.redo_post, _loaded),
        (bpy.app.handlers.load_post, _loaded),
    ):
        if callback in handlers:
            handlers.remove(callback)
