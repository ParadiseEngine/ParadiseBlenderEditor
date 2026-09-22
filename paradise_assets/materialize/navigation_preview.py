"""Transient baked navigation overlay; no scene objects or document properties."""

from __future__ import annotations

import math

import bpy
from bpy.app.handlers import persistent

_PREVIEWS = {}
_HANDLE = None


def geometry(payload):
    if not isinstance(payload, dict):
        raise ValueError("Invalid navigation preview geometry")
    vertices, indices = payload.get("vertices"), payload.get("indices")
    if not isinstance(vertices, list) or not isinstance(indices, list) or len(indices) % 3:
        raise ValueError("Invalid navigation preview geometry")
    if not vertices or not indices:
        raise ValueError("The navigation mesh contains no walkable triangles")
    if any(not isinstance(v, list) or len(v) != 3 or any(
            isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c)
            for c in v) for v in vertices):
        raise ValueError("Invalid navigation preview vertices")
    if any(type(i) is not int or not 0 <= i < len(vertices) for i in indices):
        raise ValueError("Invalid navigation preview triangle indices")
    return ([(x, -z, y + 0.025) for x, y, z in vertices],
            [tuple(indices[i:i + 3]) for i in range(0, len(indices), 3)])


def is_visible(scene):
    return scene.as_pointer() in _PREVIEWS


def show(scene, payload):
    global _HANDLE
    vertices, triangles = geometry(payload)
    batches = None
    if not bpy.app.background:
        import gpu
        from gpu_extras.batch import batch_for_shader
        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        edges = sorted({tuple(sorted(edge)) for a, b, c in triangles
                        for edge in ((a, b), (b, c), (c, a))})
        batches = (shader,
                   batch_for_shader(shader, "TRIS", {"pos": vertices}, indices=triangles),
                   batch_for_shader(shader, "LINES", {"pos": vertices}, indices=edges))
        if _HANDLE is None:
            _HANDLE = bpy.types.SpaceView3D.draw_handler_add(_draw, (), "WINDOW", "POST_VIEW")
    _PREVIEWS[scene.as_pointer()] = batches
    _redraw()


def clear(scene):
    _PREVIEWS.pop(scene.as_pointer(), None)
    _remove_unused_handler()
    _redraw()


hide = clear


def clear_all():
    _PREVIEWS.clear()
    _remove_unused_handler()
    _redraw()


def _remove_unused_handler():
    global _HANDLE
    if not _PREVIEWS and _HANDLE is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_HANDLE, "WINDOW")
        _HANDLE = None


def _draw():
    batches = _PREVIEWS.get(bpy.context.scene.as_pointer())
    if batches is None:
        return
    import gpu
    shader, faces, edges = batches
    blend, depth = gpu.state.blend_get(), gpu.state.depth_test_get()
    mask = gpu.state.depth_mask_get()
    try:
        gpu.state.blend_set("ALPHA")
        gpu.state.depth_test_set("LESS_EQUAL")
        gpu.state.depth_mask_set(False)
        shader.bind()
        shader.uniform_float("color", (0.05, 0.75, 0.6, 0.35))
        faces.draw(shader)
        shader.uniform_float("color", (0.1, 1.0, 0.8, 0.9))
        edges.draw(shader)
    finally:
        gpu.state.depth_mask_set(mask)
        gpu.state.depth_test_set(depth)
        gpu.state.blend_set(blend)


def _redraw():
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
