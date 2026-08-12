"""Export operators and the save hook."""

from __future__ import annotations

import bpy
from bpy.types import Operator

from .. import log
from ..pipeline import ktx
from ..prefs import export_paths
from .navmesh_preview import classes as navmesh_preview_classes
from .scene import export_scene

__all__ = ["classes", "register", "unregister"]


class PARADISE_OT_export_scene(Operator):
    """Export this scene to the Paradise data contract"""

    bl_idname = "paradise.export_scene"
    bl_label = "Export Paradise Scene"
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            output = export_scene(context.scene, self)
        except Exception as error:  # surface it in the UI, not a console traceback
            log.error(f"Export failed: {error}", self)
            raise

        if output is None:
            return {"CANCELLED"}
        return {"FINISHED"}


class PARADISE_OT_convert_textures(Operator):
    """Transcode textures under the data directory to KTX2 for the runtime"""

    bl_idname = "paradise.convert_textures"
    bl_label = "Convert Textures To KTX2"
    bl_options = {"REGISTER"}

    def execute(self, context):
        paths = export_paths(context.scene)
        converted, skipped = ktx.convert_data_directory(paths)
        if converted == 0 and skipped == 0:
            log.warn("No convertible textures found under the data directory.", self)
            return {"CANCELLED"}
        log.info(f"Transcoded {converted} texture(s); {skipped} already up to date.", self)
        return {"FINISHED"}


class PARADISE_OT_open_data_dir(Operator):
    """Open the export data directory in the system file browser"""

    bl_idname = "paradise.open_data_dir"
    bl_label = "Open Data Directory"

    def execute(self, context):
        paths = export_paths(context.scene)
        paths.ensure_output_directory()
        bpy.ops.wm.path_open(filepath=paths.data_dir)
        return {"FINISHED"}


@bpy.app.handlers.persistent
def _on_save_post(_file_path) -> None:
    """Re-export on save, mirroring the Godot host's save hook.

    Runs on ``save_post`` rather than ``save_pre`` for one specific reason: the export's scene
    name and its ``//``-relative data directory both resolve against ``bpy.data.filepath``,
    which is only correct *after* a Save As has completed. On ``save_pre`` a first-time save
    would write into the previous location, or into the unsaved-file fallback.

    ``@persistent`` keeps the handler alive across file loads; without it, exporting on save
    would silently stop working after the first time a .blend is opened.
    """
    for scene in bpy.data.scenes:
        settings = getattr(scene, "paradise_project", None)
        if settings is None or not settings.export_on_save:
            continue
        try:
            export_scene(scene)
        except Exception as error:  # a failed export must not break saving
            log.error(f"Export on save failed for scene '{scene.name}': {error}")


def menu_export(self, context) -> None:
    self.layout.operator(PARADISE_OT_export_scene.bl_idname, text="Paradise Scene (.json)")


classes = (
    PARADISE_OT_export_scene,
    PARADISE_OT_convert_textures,
    PARADISE_OT_open_data_dir,
    *navmesh_preview_classes,
)


def register() -> None:
    if _on_save_post not in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.append(_on_save_post)
    bpy.types.TOPBAR_MT_file_export.append(menu_export)


def unregister() -> None:
    if _on_save_post in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.remove(_on_save_post)
    bpy.types.TOPBAR_MT_file_export.remove(menu_export)
