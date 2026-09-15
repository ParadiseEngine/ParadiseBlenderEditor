"""Operators behind the Components panel's Animation Clips section.

Each clip of the GLB an object references gets a root-motion toggle and a root-bone picker;
both write the ``[glb].clips`` domain of the GLB's ``.meta`` immediately (the setting belongs
to the MODEL, not to the open document -- nothing is deferred to the document's save). The
merge and its refusals live in ``document/glb_clips.py``.
"""

from __future__ import annotations

import os

from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from bpy.types import Operator

from .document import glb_clips, mesh_document, schema
from .materialize import store

__all__ = ["classes", "glb_for_object"]


def glb_for_object(obj, project_layout) -> str | None:
    """The GLB ``obj``'s components name, resolved through its mesh document.

    Mirrors ``load._mesh_reference`` for the panel's dict-shaped component payloads: the
    first field the game's schema (or the .glb suffix fallback) calls a mesh.
    """
    mesh_fields = schema.load(project_layout.root)
    for component in store.component_json(obj):
        data = component.get("data")
        if not isinstance(data, dict):
            continue
        for field, value in data.items():
            path = value.get("path") if isinstance(value, dict) else value
            if mesh_fields.is_mesh_field(component.get("type"), field, path):
                return mesh_document.displayable(project_layout, path)
    return None


class PARADISE_ASSETS_OT_clip_root_motion(Operator):
    """Whether this clip's root bone drives the actor's root position"""

    bl_idname = "paradise_assets.clip_root_motion"
    bl_label = "Toggle Root Motion"
    # A file write cannot be undone; INTERNAL keeps it out of the operator search.
    bl_options = {"INTERNAL"}

    glb: StringProperty(name="GLB")
    index: IntProperty(name="Clip", min=0)
    enabled: BoolProperty(name="Root Motion")

    def execute(self, context):
        try:
            glb_clips.set_root_motion(self.glb, self.index, self.enabled)
        except glb_clips.ClipSettingsError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        _redraw(context)
        self.report(
            {"INFO"},
            f"{os.path.basename(self.glb)} clip {self.index}: root motion "
            f"{'on' if self.enabled else 'off'} — written to its .meta",
        )
        return {"FINISHED"}


def _bone_items(self, context):
    """The GLB's skin joints, for the picker's search popup."""
    glb = getattr(self, "glb", "") or _LAST_GLB[0]
    info = glb_clips.rig(glb) if glb else None
    joints = info.joints if info is not None else ()
    # The callback's tuples are not retained, so build a fresh list per call but only when
    # the joint set moved (the popup re-polls it while it is open).
    global _BONE_CACHE
    if _BONE_CACHE[0] != joints:
        _BONE_CACHE = (joints, [(name, name, "") for name in joints])
    return _BONE_CACHE[1] or [("NONE", "(no joints in this GLB)", "")]


#: (joints, items) the last items call built; enum items tuples are not retained by Blender.
_BONE_CACHE: tuple = ((), [])
#: The GLB the open picker is for: ``self.glb`` normally carries it; this is the fallback for
#: any evaluation path where the operator's own properties are not in hand.
_LAST_GLB: list = [""]


class PARADISE_ASSETS_OT_clip_root_bone(Operator):
    """The bone whose motion is lifted onto the actor; empty auto-detects the skin root"""

    bl_idname = "paradise_assets.clip_root_bone"
    bl_label = "Pick Root Bone"
    bl_property = "bone"
    bl_options = {"INTERNAL"}

    glb: StringProperty(name="GLB")
    index: IntProperty(name="Clip", min=0)
    bone: EnumProperty(name="Root Bone", items=_bone_items)
    #: The row's clear button runs the same operator with this set -- an EnumProperty cannot
    #: be assigned "", so "back to auto-detect" is its own gesture rather than an enum item.
    auto: BoolProperty(name="Auto-detect", default=False, options={"HIDDEN"})

    def invoke(self, context, event):
        if self.auto:
            return self.execute(context)
        _LAST_GLB[0] = self.glb
        context.window_manager.invoke_search_popup(self)
        return {"FINISHED"}

    def execute(self, context):
        bone = "" if self.auto or self.bone == "NONE" else self.bone
        try:
            glb_clips.set_root_bone(self.glb, self.index, bone)
        except glb_clips.ClipSettingsError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        _redraw(context)
        picked = bone or "auto (the skin's root joint)"
        self.report(
            {"INFO"},
            f"{os.path.basename(self.glb)} clip {self.index}: root bone {picked} — "
            "written to its .meta",
        )
        return {"FINISHED"}


def _redraw(context) -> None:
    """Repaint the sidebar: a sidecar write is not a Blender change, so nothing else tags it."""
    screen = getattr(context, "screen", None)
    if screen is None:
        return
    for area in screen.areas:
        area.tag_redraw()


classes = (
    PARADISE_ASSETS_OT_clip_root_motion,
    PARADISE_ASSETS_OT_clip_root_bone,
)
