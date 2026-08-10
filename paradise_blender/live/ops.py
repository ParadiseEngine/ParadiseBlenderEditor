"""Live-preview operators."""

from __future__ import annotations

from bpy.types import Operator

from . import session

__all__ = ["classes"]


class PARADISE_OT_live_start(Operator):
    """Launch the runtime and stream scene edits to it as you work"""

    bl_idname = "paradise.live_start"
    bl_label = "Start Live Preview"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context) -> bool:
        return not session.is_running()

    def execute(self, context):
        return {"FINISHED"} if session.start(context.scene, self) else {"CANCELLED"}


class PARADISE_OT_live_stop(Operator):
    """Stop the live preview session"""

    bl_idname = "paradise.live_stop"
    bl_label = "Stop Live Preview"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context) -> bool:
        return session.current_session() is not None

    def execute(self, context):
        return {"FINISHED"} if session.stop(self) else {"CANCELLED"}


class PARADISE_OT_live_resync(Operator):
    """Push the whole scene to the running preview"""

    bl_idname = "paradise.live_resync"
    bl_label = "Resync Live Preview"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context) -> bool:
        return session.is_running()

    def execute(self, context):
        active = session.current_session()
        if active is None:
            return {"CANCELLED"}
        active.send_full_scene(context.scene)
        self.report({"INFO"}, "Resynced the live preview.")
        return {"FINISHED"}


classes = (PARADISE_OT_live_start, PARADISE_OT_live_stop, PARADISE_OT_live_resync)
