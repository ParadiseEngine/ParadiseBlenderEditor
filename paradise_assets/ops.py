"""The operators: open a prefab document, save it back, reload it.

The explicit save operator predates sync-on-save; both paths exist now. Ctrl+S goes through
``materialize/sync.py`` (a ``save_pre`` handler, so the document is written before the .blend
and a refusal can be recorded for the panel). The operator remains for saving the document
without saving the working file, and as the path with a report channel.
"""

from __future__ import annotations

import contextlib
import os
import subprocess

import bpy
from bpy.props import StringProperty
from bpy.types import Operator

from . import catalogue, watch
from .document import project
from .document.prefab import PrefabDocumentError, loads
from .materialize import instancing, load, save, store, workfile

__all__ = ["classes"]


def _start_watch(operator: Operator, layout: project.ProjectLayout) -> None:
    """Start this project's watcher, reporting a setup gap rather than failing the open."""
    problem = watch.start_for(layout.root)
    if problem is not None:
        operator.report({"WARNING"}, problem)


class PARADISE_ASSETS_OT_open_prefab(Operator):
    """Open a Paradise prefab document and materialize it as Blender objects"""

    bl_idname = "paradise_assets.open_prefab"
    bl_label = "Open Prefab Document"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")  # type: ignore[valid-type]
    filter_glob: StringProperty(default="*.prefab", options={"HIDDEN"})  # type: ignore[valid-type]

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

        # The working file first: it holds the session (camera, selection), and load_post
        # rematerializes the objects.
        if workfile.try_open(layout, path):
            _start_watch(self, layout)
            self.report(
                {"INFO"},
                f"Opened {os.path.basename(path)} from its working file "
                f"({os.path.relpath(workfile.path_for(layout, path), layout.root)})",
            )
            return {"FINISHED"}

        try:
            with open(path, encoding="utf-8") as handle:
                document = loads(handle.read(), path)
        except PrefabDocumentError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        except OSError as error:
            self.report({"ERROR"}, f"Could not read {path}: {error}")
            return {"CANCELLED"}

        # try_open may have replaced the session; use the scene we have NOW.
        result = load.load_document(bpy.context.scene, document, path, layout)
        for warning in result.warnings[:5]:
            self.report({"WARNING"}, warning)

        written = workfile.save(layout, path)
        if written is None:
            self.report({"WARNING"}, "could not write the working file under .editor/blend")

        # After the open, and a warning only: "no CLI installed" is no reason to refuse a file.
        _start_watch(self, layout)

        self.report(
            {"INFO"},
            f"Opened {os.path.basename(path)}: {result.objects} object(s), "
            f"{result.meshes} mesh(es)"
            + (f", {len(result.warnings)} warning(s)" if result.warnings else ""),
        )
        return {"FINISHED"}


class PARADISE_ASSETS_OT_reload_prefab(Operator):
    """Re-read the prefab document, discarding changes made here"""

    bl_idname = "paradise_assets.reload_prefab"
    bl_label = "Reload Prefab Document"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return store.read_state(context.scene) is not None

    def execute(self, context):
        state = store.read_state(context.scene)
        try:
            with open(state.path, encoding="utf-8") as handle:
                document = loads(handle.read(), state.path)
        except (PrefabDocumentError, OSError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        # Never the working file: reload's job is to discard this session.
        layout = project.locate(state.path)
        result = load.load_document(context.scene, document, state.path, layout)
        workfile.save(layout, state.path)

        self.report({"INFO"}, f"Reloaded {result.objects} object(s)")
        return {"FINISHED"}


class PARADISE_ASSETS_OT_save_prefab(Operator):
    """Write placement changes back to the prefab document"""

    bl_idname = "paradise_assets.save_prefab"
    bl_label = "Save to Prefab Document"

    @classmethod
    def poll(cls, context):
        return store.read_state(context.scene) is not None

    def execute(self, context):
        try:
            result = save.save_prefab(context.scene)
        except save.SaveError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        except OSError as error:
            self.report({"ERROR"}, f"Could not write the document: {error}")
            return {"CANCELLED"}

        for warning in result.warnings[:5]:
            self.report({"WARNING"}, warning)

        # Re-record the new stamp, or the next open rebuilds a workfile that only looks stale.
        state = store.read_state(context.scene)
        if state is not None and (layout := project.locate(state.path)) is not None:
            workfile.save(layout, state.path)

        changes = []
        if result.moved:
            changes.append(f"{result.moved} moved")
        if result.added:
            changes.append(f"{result.added} added")
        if result.removed:
            changes.append(f"{result.removed} removed")
        if result.edited:
            changes.append(f"{result.edited} field(s) edited")
        self.report(
            {"INFO"},
            f"Saved {result.written} object(s)"
            + (f" ({', '.join(changes)})" if changes else " (no placement changes)"),
        )
        return {"FINISHED"}


class PARADISE_ASSETS_OT_toggle_watch(Operator):
    """Start or stop the asset watcher for this document's project"""

    bl_idname = "paradise_assets.toggle_watch"
    bl_label = "Toggle Asset Watch"

    @classmethod
    def poll(cls, context):
        return store.read_state(context.scene) is not None

    def execute(self, context):
        state = store.read_state(context.scene)
        layout = project.locate(state.path)
        if layout is None:
            self.report({"ERROR"}, "No asset project for the open document")
            return {"CANCELLED"}

        if watch.is_running(layout.root):
            watch.stop(layout.root)
            self.report({"INFO"}, "Asset watch stopped")
            return {"FINISHED"}

        # `start`, not `start_for`: an explicit button must not be silenced by the preference.
        problem = watch.start(layout.root)
        if problem is not None:
            self.report({"ERROR"}, problem)
            return {"CANCELLED"}
        self.report({"INFO"}, "Asset watch started")
        return {"FINISHED"}


class PARADISE_ASSETS_OT_add_prefab_instance(Operator):
    """Place an instance of a prefab in the open document"""

    bl_idname = "paradise_assets.add_prefab_instance"
    bl_label = "Add Prefab Instance"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")  # type: ignore[valid-type]
    filter_glob: StringProperty(default="*.prefab", options={"HIDDEN"})  # type: ignore[valid-type]

    @classmethod
    def poll(cls, context):
        return store.read_state(context.scene) is not None

    def invoke(self, context, event):
        if self.filepath:
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        state = store.read_state(context.scene)
        if state is None:
            self.report({"ERROR"}, "No prefab document is open — open one before adding to it.")
            return {"CANCELLED"}

        path = os.path.abspath(bpy.path.abspath(self.filepath))
        if not os.path.isfile(path):
            self.report({"ERROR"}, f"No such file: {path}")
            return {"CANCELLED"}

        if os.path.normcase(path) == os.path.normcase(state.path):
            # The resolver would catch the cycle later; here it says so at the moment it happens.
            self.report({"ERROR"}, "A document cannot instantiate itself.")
            return {"CANCELLED"}

        layout = project.locate(path)
        if layout is None:
            self.report({"ERROR"}, f"No asset project above {path}")
            return {"CANCELLED"}

        try:
            added = instancing.add_instance(context.scene, path, layout, tuple(context.scene.cursor.location))
        except instancing.InstanceError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        # Selected and active: the next thing anyone does is move it.
        for obj in context.selected_objects:
            obj.select_set(False)
        added.select_set(True)
        context.view_layer.objects.active = added

        self.report({"INFO"}, f"Added '{added.name}'. Save to write it to the document.")
        return {"FINISHED"}


class PARADISE_ASSETS_OT_refresh_catalogue(Operator):
    """Regenerate the Asset Browser catalogue of this project's prefabs"""

    bl_idname = "paradise_assets.refresh_catalogue"
    bl_label = "Refresh Prefab Catalogue"

    @classmethod
    def poll(cls, context):
        return store.read_state(context.scene) is not None

    def execute(self, context):
        state = store.read_state(context.scene)
        layout = project.locate(state.path)
        if layout is None:
            self.report({"ERROR"}, "No asset project found for the open document")
            return {"CANCELLED"}

        # Own process: building the catalogue REPLACES the current file. __package__, not a
        # literal: an extension's module is bl_ext.<repo>.paradise_assets. Blocks the UI (#36).
        script = (
            "import importlib;"
            f"c=importlib.import_module('{__package__}.catalogue');"
            f"print('CATALOGUE', *c.build(r'{layout.root}'))"
        )
        result = subprocess.run(
            [bpy.app.binary_path, "--background", "--python-expr", script],
            # A cold cache renders ~0.5 s per prefab, so a large project's first build is minutes.
            capture_output=True, text=True, timeout=1800,
        )

        if result.returncode != 0:
            self.report({"ERROR"}, f"Catalogue build failed: {result.stderr.strip()[-300:]}")
            return {"CANCELLED"}

        fields = next(
            (line.split() for line in result.stdout.splitlines() if line.startswith("CATALOGUE")),
            [],
        )
        made = fields[1] if len(fields) > 1 else "?"
        pictured = fields[2] if len(fields) > 2 else "?"

        # Register HERE: a preferences change in the background Blender dies with it, and an
        # unregistered catalogue is a file nothing looks at (how it first shipped).
        name, added = catalogue.ensure_library(layout.root)
        if added:
            bpy.ops.wm.save_userpref()

        with contextlib.suppress(RuntimeError):
            bpy.ops.asset.library_refresh()

        self.report(
            {"INFO"},
            f"Catalogue rebuilt with {made} prefab(s), {pictured} with thumbnails — "
            + (f"registered the '{name}' asset library" if added else f"library '{name}' refreshed")
            + ". Open an Asset Browser and pick it from the library dropdown.",
        )
        return {"FINISHED"}


class PARADISE_ASSETS_FH_prefab(bpy.types.FileHandler):
    """Drag a ``.prefab`` from the file browser into the viewport to instance it."""

    bl_idname = "PARADISE_ASSETS_FH_prefab"
    bl_label = "Paradise Prefab"
    bl_import_operator = "paradise_assets.add_prefab_instance"
    bl_file_extensions = ".prefab"

    @classmethod
    def poll_drop(cls, context):
        # Only with a document open, or the drop looks accepted and then errors.
        return (
            context.area is not None
            and context.area.type == "VIEW_3D"
            and store.read_state(context.scene) is not None
        )


classes = (
    PARADISE_ASSETS_OT_open_prefab,
    PARADISE_ASSETS_OT_reload_prefab,
    PARADISE_ASSETS_OT_save_prefab,
    PARADISE_ASSETS_OT_toggle_watch,
    PARADISE_ASSETS_OT_add_prefab_instance,
    PARADISE_ASSETS_OT_refresh_catalogue,
    PARADISE_ASSETS_FH_prefab,
)
