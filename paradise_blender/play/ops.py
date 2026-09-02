"""The Play operator."""

from __future__ import annotations

import os
import time

from bpy.types import Operator

from .. import log
from ..export.scene import export_scene, resolve_scene_name
from ..prefs import export_paths
from .host import first_error_line, launch_runtime, log_path

__all__ = ["classes"]

# Three minutes: thirty seconds was not enough (a restore that cannot reach nuget.org spends
# ~75 s timing out), and the failure was the worst kind: watch expired, success reported, build
# died a minute later unheard. Waiting is free. It bounds the failure rather than removing it:
# Blender cannot see whether a window opened.
WATCH_SECONDS = 180.0
POLL_INTERVAL = 0.4


class PARADISE_OT_play(Operator):
    """Launch the exported scene in the standalone Paradise runtime"""

    bl_idname = "paradise.play"
    bl_label = "Play in Paradise"
    bl_options = {"REGISTER"}

    export_first: __import__("bpy").props.BoolProperty(  # type: ignore[valid-type]
        name="Export First",
        description=(
            "Re-export before launching. Off by default: data/ is authoring output kept fresh "
            "by the save hook, and launching is a pure consumer of it"
        ),
        default=False,
    )

    # Plain attributes, not RNA properties: watch state must not reach the redo panel or a keymap.
    _process = None
    _timer = None
    _deadline = 0.0

    def execute(self, context):
        scene = context.scene
        paths = export_paths(scene)

        if self.export_first:
            export_scene(scene, self)

        scene_json = paths.level_data_output_path(resolve_scene_name(scene))
        if not os.path.exists(scene_json):
            log.error(
                f"'{scene_json}' does not exist. Save the .blend (auto-export) or run "
                "Export Paradise Scene first.",
                self,
            )
            return {"CANCELLED"}

        # In the project root, not wherever Blender is (see launch_runtime).
        process = launch_runtime(["--scene", scene_json], self, cwd=paths.project_root)
        if process is None:
            return {"CANCELLED"}

        # A detached runtime's death would otherwise show up as a pid and nothing else.
        # Background Blender cannot run a modal, so a scripted play() reports success.
        if context.window is None:
            return {"FINISHED"}

        self._process = process
        self._deadline = time.monotonic() + WATCH_SECONDS
        window_manager = context.window_manager
        self._timer = window_manager.event_timer_add(POLL_INTERVAL, window=context.window)
        window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        code = self._process.poll()
        if code is None:
            # Alive past the window: its lifetime is the player's business now.
            return self._release(context) if time.monotonic() >= self._deadline else {"PASS_THROUGH"}

        if code == 0:
            return self._release(context)

        detail = first_error_line(log_path())
        log.error(
            f"Runtime exited with code {code}: {detail}"
            if detail
            else f"Runtime exited with code {code} — see {log_path()}",
            self,
        )
        self._release(context)
        return {"CANCELLED"}

    def cancel(self, context) -> None:
        self._release(context)

    def _release(self, context) -> set[str]:
        """Drop the timer. Safe to call twice -- ``cancel`` also runs on a modal that finished."""
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        return {"FINISHED"}


classes = (PARADISE_OT_play,)
