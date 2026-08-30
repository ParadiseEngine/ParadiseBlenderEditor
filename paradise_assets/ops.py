"""The operators: open a scene document, save it back, reload it.

Saving is an EXPLICIT operator rather than a ``save_post`` handler. Sync-on-save is where §2.7 of
the asset-management plan ends up, and it should land once the round trip has been proven on real
content -- wiring it now would mean every experimental edit writes to the committed source of
truth, including the ones made to see what something looks like.
"""

from __future__ import annotations

import os

import bpy
from bpy.props import StringProperty
from bpy.types import Operator

from .document import project
from .document.scene import SceneDocumentError, loads
from .materialize import load, save, store

__all__ = ["classes"]


class PARADISE_ASSETS_OT_open_scene(Operator):
    """Open a Paradise scene document and materialize it as Blender objects"""

    bl_idname = "paradise_assets.open_scene"
    bl_label = "Open Scene Document"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")  # type: ignore[valid-type]
    filter_glob: StringProperty(default="*.scene", options={"HIDDEN"})  # type: ignore[valid-type]

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        path = os.path.abspath(bpy.path.abspath(self.filepath))
        if not os.path.isfile(path):
            self.report({"ERROR"}, f"No such file: {path}")
            return {"CANCELLED"}

        layout = project.locate(path)
        if layout is None:
            self.report(
                {"ERROR"},
                f"No asset project at or above {path}: expected an "
                f"{project.ASSETS_DIR}/{project.MANIFEST_NAME} in some parent directory.",
            )
            return {"CANCELLED"}

        try:
            with open(path, encoding="utf-8") as handle:
                document = loads(handle.read(), path)
        except SceneDocumentError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        except OSError as error:
            self.report({"ERROR"}, f"Could not read {path}: {error}")
            return {"CANCELLED"}

        result = load.load_document(context.scene, document, path, layout)
        for warning in result.warnings[:5]:
            self.report({"WARNING"}, warning)

        self.report(
            {"INFO"},
            f"Opened {os.path.basename(path)}: {result.objects} object(s), "
            f"{result.meshes} mesh(es)"
            + (f", {len(result.warnings)} warning(s)" if result.warnings else ""),
        )
        return {"FINISHED"}


class PARADISE_ASSETS_OT_reload_scene(Operator):
    """Re-read the scene document, discarding changes made here"""

    bl_idname = "paradise_assets.reload_scene"
    bl_label = "Reload Scene Document"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return store.read_state(context.scene) is not None

    def execute(self, context):
        state = store.read_state(context.scene)
        try:
            with open(state.path, encoding="utf-8") as handle:
                document = loads(handle.read(), state.path)
        except (SceneDocumentError, OSError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        layout = project.locate(state.path)
        result = load.load_document(context.scene, document, state.path, layout)
        self.report({"INFO"}, f"Reloaded {result.objects} object(s)")
        return {"FINISHED"}


class PARADISE_ASSETS_OT_save_scene(Operator):
    """Write placement changes back to the scene document"""

    bl_idname = "paradise_assets.save_scene"
    bl_label = "Save to Scene Document"

    @classmethod
    def poll(cls, context):
        return store.read_state(context.scene) is not None

    def execute(self, context):
        try:
            result = save.save_scene(context.scene)
        except save.SaveError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        except OSError as error:
            self.report({"ERROR"}, f"Could not write the document: {error}")
            return {"CANCELLED"}

        for warning in result.warnings[:5]:
            self.report({"WARNING"}, warning)

        changes = []
        if result.moved:
            changes.append(f"{result.moved} moved")
        if result.added:
            changes.append(f"{result.added} added")
        if result.removed:
            changes.append(f"{result.removed} removed")
        self.report(
            {"INFO"},
            f"Saved {result.written} object(s)"
            + (f" ({', '.join(changes)})" if changes else " (no placement changes)"),
        )
        return {"FINISHED"}


classes = (
    PARADISE_ASSETS_OT_open_scene,
    PARADISE_ASSETS_OT_reload_scene,
    PARADISE_ASSETS_OT_save_scene,
)
