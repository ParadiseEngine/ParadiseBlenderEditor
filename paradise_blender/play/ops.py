"""The Play operator."""

from __future__ import annotations

import os

from bpy.types import Operator

from .. import log
from ..export.scene import export_scene, resolve_scene_name
from ..prefs import export_paths
from .host import launch_runtime

__all__ = ["classes"]


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

        pid = launch_runtime(["--scene", scene_json], self)
        return {"FINISHED"} if pid is not None else {"CANCELLED"}


classes = (PARADISE_OT_play,)
