"""The operators: open a prefab document, save it back, reload it.

Saving is an EXPLICIT operator rather than a ``save_post`` handler. Sync-on-save is where §2.7 of
the asset-management plan ends up, and it should land once the round trip has been proven on real
content -- wiring it now would mean every experimental edit writes to the committed source of
truth, including the ones made to see what something looks like.
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

        # The working file first: if there is one and the document has not moved on, it IS this
        # document materialized, plus the camera and selection the last session left. Re-reading
        # the document instead would rebuild the same objects and throw that away.
        if workfile.try_open(layout, path):
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

        # try_open may have replaced the session with a stale working file, so the scene to
        # materialize into is whatever we have NOW rather than the one this started with.
        result = load.load_document(bpy.context.scene, document, path, layout)
        for warning in result.warnings[:5]:
            self.report({"WARNING"}, warning)

        written = workfile.save(layout, path)
        if written is None:
            self.report({"WARNING"}, "could not write the working file under .editor/blend")

        # After the document is open, not before: a watcher for a project whose document failed
        # to load is a rebuild nobody asked for. Reported as a WARNING rather than failing the
        # open -- the document is loaded and usable, and "no CLI installed" is a setup gap rather
        # than a reason to refuse the file.
        problem = watch.start_for(layout.root)
        if problem is not None:
            self.report({"WARNING"}, problem)

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

        # Reload deliberately does NOT consult the working file: its whole job is to discard what
        # this session did and go back to the document, and the working file is this session's.
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

        # The document moved, so its stamp did too. Rewriting the working file re-records the new
        # stamp; without this the next open would find a workfile that looks stale against a
        # document it is in fact identical to, and rebuild it for nothing.
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

        # `start`, not `start_for`: this button is an explicit request, and an author who turned
        # the automatic behaviour off may still want one for this session. A button that silently
        # did nothing because of a preference is the worst of both.
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
        # Dropped files arrive with filepath already set; a menu invocation has to ask.
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
            # Not a rule of the format -- the resolver would catch the cycle -- but catching it
            # here says what went wrong at the moment it went wrong.
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

        # Selected and active, because a dropped object you then have to hunt for is worse than
        # no drop at all -- and because the next thing anyone does is move it.
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

        # Building the catalogue REPLACES the current Blender file, so it runs in its own process.
        # Doing it in-process would throw away whatever the author has open, which is not a trade
        # a button labelled "refresh" is allowed to make.
        # __package__ rather than a literal: this addon is installed as an extension, so its module
        # is bl_ext.<repo>.paradise_assets and the repo name is whatever the user called it.
        script = (
            "import importlib;"
            f"c=importlib.import_module('{__package__}.catalogue');"
            f"print('CATALOGUE', *c.build(r'{layout.root}'))"
        )
        result = subprocess.run(
            [bpy.app.binary_path, "--background", "--python-expr", script],
            # Generous because the build now RENDERS a thumbnail per prefab on a cold cache --
            # measured at roughly half a second each, so a large project's first build is minutes.
            # Later builds re-render only what changed and come back in seconds.
            capture_output=True, text=True, timeout=1800,
        )

        if result.returncode != 0:
            self.report({"ERROR"}, f"Catalogue build failed: {result.stderr.strip()[-300:]}")
            return {"CANCELLED"}

        # 'CATALOGUE <made> <pictured> [warnings]' -- build() returns three values and the print
        # unpacks them, so the counts are fields 1 and 2.
        fields = next(
            (line.split() for line in result.stdout.splitlines() if line.startswith("CATALOGUE")),
            [],
        )
        made = fields[1] if len(fields) > 1 else "?"
        pictured = fields[2] if len(fields) > 2 else "?"

        # Registering happens HERE, in the running session, not in the subprocess that built the
        # file: a preferences change made in a background Blender dies with it. Without this the
        # catalogue is a file nothing looks at, which is exactly how it first shipped.
        name, added = catalogue.ensure_library(layout.root)
        if added:
            bpy.ops.wm.save_userpref()

        # So it appears without restarting Blender.
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
        # Only into a 3D viewport, and only when there is a document for it to go into --
        # otherwise the drop would look accepted and then report an error.
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
