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

# How long to keep watching a launched runtime for an early death. It has to outlast a cold
# `dotnet run`, whose build can take several seconds before the failure surfaces -- the whole
# point is to catch build errors, and those are the slow ones.
#
# THREE MINUTES, because thirty seconds was not enough and the way it failed was the worst
# available: the watch expired, the operator reported success, and the build died a minute later
# with nobody listening. "Play does nothing and says nothing" is a far harder thing to diagnose
# than any error message, and it is what this constant produces whenever it is too small.
#
# The slow cases are not slow because they are big. A restore that cannot reach nuget.org spends
# ~75 s in connection timeouts before failing (NU1900 is an error here -- both repos set
# TreatWarningsAsErrors), and a cold build after a package bump is minutes. Waiting is free: the
# timer polls a `poll()` and passes every other event straight through, so a long window costs
# nothing and buys the error message. Launch success is reported immediately by `launch_runtime`,
# not at the end of the watch, so widening it costs the author no perceived latency either.
#
# IT BOUNDS THE FAILURE RATHER THAN REMOVING IT. The architecture is still "no death within the
# window means success", so a build slower than three minutes reports success and then dies
# unheard -- the same shape, further away. Removing it properly means watching until the process
# either dies or is observed to have opened a window, and Blender cannot see the second half.
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

    # Plain Python attributes, not RNA properties: they hold the watch state for one modal run
    # and must not be saved, presented in the redo panel, or set from a keymap.
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

        # IN THE PROJECT ROOT, not in whatever directory Blender happens to have.
        #
        # ``--scene`` is absolute, so the scene is always found; everything else a runtime reads
        # is its own business and is conventionally relative to the directory holding ``data/``.
        # That directory is exactly ``project_root`` -- the Blender analogue of Godot's ``res://``
        # -- so it is what the child gets. See ``launch_runtime`` for the failure this prevents.
        process = launch_runtime(["--scene", scene_json], self, cwd=paths.project_root)
        if process is None:
            return {"CANCELLED"}

        # Watch the child instead of declaring victory on a successful fork. A detached runtime
        # writes its output to a log file, so a build error or a missing asset would otherwise
        # show up in Blender as "Launched Paradise runtime (pid N)" and nothing else.
        #
        # Background Blender has no window to hang a timer on, and modal operators never run
        # there anyway. Launching still succeeded, so report success rather than failing a
        # scripted `bpy.ops.paradise.play()` over a diagnostic it could not have shown.
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
            # Still alive past the watch window: it built and opened a window, and its lifetime
            # is now the player's business, not Blender's.
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
