"""Live-preview operators. Starting is modal: the runtime takes up to a minute to open its
port, and waiting for it on the main thread froze Blender for all of it."""

from __future__ import annotations

import bpy
from bpy.types import Operator

from . import session

__all__ = ["classes"]

_POLL_SECONDS = 0.25


class PARADISE_OT_live_start(Operator):
    """Launch the runtime and stream scene edits to it as you work"""

    bl_idname = "paradise.live_start"
    bl_label = "Start Live Preview"
    bl_options = {"REGISTER"}

    _startup = None
    _timer = None

    @classmethod
    def poll(cls, context) -> bool:
        return not session.is_running()

    def execute(self, context):
        # Background Blender has no event loop for a modal; scripts get the blocking start.
        if bpy.app.background or context.window is None:
            return {"FINISHED"} if session.start(context.scene, self) else {"CANCELLED"}

        self._startup = session.begin(context.scene, self)
        if self._startup is None:
            return {"CANCELLED"}
        window_manager = context.window_manager
        self._timer = window_manager.event_timer_add(_POLL_SECONDS, window=context.window)
        window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        self._startup.step()
        if not self._startup.done:
            return {"PASS_THROUGH"}
        self._release(context)
        return {"CANCELLED"} if self._startup.failed else {"FINISHED"}

    def cancel(self, context) -> None:
        self._release(context)

    def _release(self, context) -> None:
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None


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
