"""Transient viewport overlays returned by authored actions; never document objects."""

from __future__ import annotations

import bpy
from bpy.app.handlers import persistent

_OVERLAYS = {}
_HANDLE = None


def apply(scene, owner, overlays):
    global _HANDLE
    key = scene.as_pointer()
    for overlay in overlays:
        address = (key, owner, overlay.id)
        if not overlay.visible or not overlay.triangles:
            _OVERLAYS.pop(address, None)
            continue
        batches = None
        if not bpy.app.background:
            import gpu
            from gpu_extras.batch import batch_for_shader
            shader = gpu.shader.from_builtin("UNIFORM_COLOR")
            batches = (shader, batch_for_shader(shader, "TRIS", {"pos": overlay.vertices},
                                                indices=overlay.triangles), overlay.color)
            if _HANDLE is None:
                _HANDLE = bpy.types.SpaceView3D.draw_handler_add(_draw, (), "WINDOW", "POST_VIEW")
        _OVERLAYS[address] = batches
    _remove_unused_handler()
    redraw()


def is_visible(scene, owner, overlay_id):
    return (scene.as_pointer(), owner, overlay_id) in _OVERLAYS


def clear(scene):
    key = scene.as_pointer()
    for address in list(_OVERLAYS):
        if address[0] == key:
            del _OVERLAYS[address]
    _remove_unused_handler()
    redraw()


def prune(scene, owners):
    key = scene.as_pointer()
    removed = False
    for address in list(_OVERLAYS):
        if address[0] == key and address[1] not in owners:
            del _OVERLAYS[address]
            removed = True
    if removed:
        _remove_unused_handler()
        redraw()


def clear_all():
    _OVERLAYS.clear()
    _remove_unused_handler()
    redraw()


def _remove_unused_handler():
    global _HANDLE
    if not _OVERLAYS and _HANDLE is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_HANDLE, "WINDOW")
        _HANDLE = None


def _draw():
    import gpu
    scene_key = bpy.context.scene.as_pointer()
    blend, depth = gpu.state.blend_get(), gpu.state.depth_test_get()
    mask = gpu.state.depth_mask_get()
    try:
        gpu.state.blend_set("ALPHA")
        gpu.state.depth_test_set("LESS_EQUAL")
        gpu.state.depth_mask_set(False)
        for (key, _owner, _id), batches in _OVERLAYS.items():
            if key != scene_key or batches is None:
                continue
            shader, faces, color = batches
            shader.bind()
            shader.uniform_float("color", color)
            faces.draw(shader)
    finally:
        gpu.state.depth_mask_set(mask)
        gpu.state.depth_test_set(depth)
        gpu.state.blend_set(blend)


def redraw():
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


@persistent
def _loaded(*_):
    clear_all()


def register_handler():
    if _loaded not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_loaded)


def unregister_handler():
    if _loaded in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_loaded)
    clear_all()
