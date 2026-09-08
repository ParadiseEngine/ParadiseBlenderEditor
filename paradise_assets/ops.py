"""The operators: open a prefab document, save it back, reload it, and create new ones.

The explicit save operator predates sync-on-save; both paths exist now. Ctrl+S goes through
``materialize/sync.py`` (a ``save_pre`` handler, so the document is written before the .blend
and a refusal can be recorded for the panel). The operator remains for saving the document
without saving the working file, and as the path with a report channel.

The two that CREATE prefabs are thin: extraction is document surgery (``document/extract.py``)
order those steps have to happen in, which is the part a Blender session can get wrong -- reach
the file before reading it, mint the identity before the document that references it, and
rematerialize afterwards so the viewport shows what was written.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile

import bpy
from bpy.props import StringProperty
from bpy.types import Operator

from . import catalogue, watch
from .document import apply as apply_overrides
from .document import atomic, extract, geometry_prefab, new_prefab, overrides, project, unpack
from .document import prefab as prefab_document
from .document.prefab import PrefabDocumentError, loads
from .materialize import geometry, grouping, instancing, load, save, store, workfile
from .play import host

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
    # No UNDO: this replaces the session with a file; an undo step over it is a lie.
    bl_options = {"REGISTER"}

    filepath: StringProperty(subtype="FILE_PATH")  # type: ignore[valid-type]
    filter_glob: StringProperty(default="*.prefab", options={"HIDDEN"})  # type: ignore[valid-type]

    def invoke(self, context, event):
        # A panel row for a specific prefab sets ``filepath`` and means "open THAT" -- putting a
        # file browser in front of it would ask the author to pick the file they just clicked.
        # Panel buttons get fresh property defaults per draw, so the plain Open… button (which
        # sets nothing) still browses.
        if self.filepath:
            return self.execute(context)
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

        # try_open may have replaced the session; use the scene we have NOW. clear_startup:
        # this is the one path a person takes to open a document into a Blender that has nothing
        # else in it, and the startup file's cube is not part of their level.
        result = load.load_document(
            bpy.context.scene, document, path, layout, clear_startup=True)
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
    bl_options = {"REGISTER"}

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


class PARADISE_ASSETS_OT_recreate_workfile(Operator):
    """Delete this document's working .blend and build a new one from the document"""

    bl_idname = "paradise_assets.recreate_workfile"
    bl_label = "Recreate Working File"
    # No UNDO: it deletes a file and replaces the session; an undo step over that is a lie.
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return store.read_state(context.scene) is not None

    def invoke(self, context, event):
        # Always asks. Reload keeps what the workfile holds and this does not, so the difference
        # between the two buttons is exactly what an author would lose by picking the wrong one.
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        state = store.read_state(context.scene)
        if state is None:
            self.report({"ERROR"}, "No prefab document is open.")
            return {"CANCELLED"}

        document_path = state.path
        layout = project.locate(document_path)
        if layout is None:
            self.report({"ERROR"}, f"No asset project at or above {document_path}")
            return {"CANCELLED"}

        # Read BEFORE deleting anything. A document that will not parse must leave the author
        # with the working file they still had, not with neither it nor a scene.
        try:
            with open(document_path, encoding="utf-8") as handle:
                document = loads(handle.read(), document_path)
        except (PrefabDocumentError, OSError) as error:
            self.report({"ERROR"}, f"{error} — the working file was left alone.")
            return {"CANCELLED"}

        discarded = _discard_workfile(layout, document_path)

        # An empty file, not the startup one: this is a rebuild of a cache, and the cube would
        # only have to be cleared again. `read_homefile`, never `read_factory_settings`, which
        # resets preferences and so disables this addon mid-operator.
        bpy.ops.wm.read_homefile(use_empty=True)

        # The session was replaced; use the scene we have NOW.
        scene = bpy.context.scene
        result = load.load_document(scene, document, document_path, layout)
        for warning in result.warnings[:5]:
            self.report({"WARNING"}, warning)

        written = workfile.save(layout, document_path)
        if written is None:
            self.report({"WARNING"}, "could not write the working file under .editor/blend")

        _start_watch(self, layout)
        self.report(
            {"INFO"},
            f"Recreated {os.path.basename(document_path)} from the document: "
            f"{result.objects} object(s), {result.meshes} mesh(es)"
            + (f"; discarded {discarded} cached file(s)" if discarded else ""),
        )
        return {"FINISHED"}


def _discard_workfile(layout: project.ProjectLayout, document_path: str) -> int:
    """Remove the working file and Blender's own backup beside it; how many went.

    The backup goes too, or `.blend1` is a copy of exactly the state being thrown away, one
    File > Recover Last Session from coming back.
    """
    target = workfile.path_for(layout, document_path)
    gone = 0
    for path in (target, target + "1"):
        with contextlib.suppress(OSError):
            os.remove(path)
            gone += 1
    return gone


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
    """Start or stop the asset watcher for this project"""

    bl_idname = "paradise_assets.toggle_watch"
    bl_label = "Toggle Asset Watch"

    @classmethod
    def poll(cls, context):
        return store.project_of(context.scene) is not None

    def execute(self, context):
        layout = store.project_of(context.scene)
        if layout is None:
            self.report({"ERROR"}, "No asset project for this file")
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


class PARADISE_ASSETS_OT_create_prefab(Operator):
    """Save selected static meshes as a reusable prefab, keeping the source Blender scene"""

    bl_idname = "paradise_assets.create_prefab"
    bl_label = "Create Prefab from Selection"
    bl_options = {"REGISTER"}

    filepath: StringProperty(subtype="FILE_PATH")  # type: ignore[valid-type]
    filter_glob: StringProperty(default="*.prefab", options={"HIDDEN"})  # type: ignore[valid-type]

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and any(
            obj.type == "MESH" and store.guid_of(obj) is None for obj in context.selected_objects
        )

    def invoke(self, context, event):
        layout = store.project_of(context.scene)
        if layout is not None and not self.filepath:
            name = bpy.path.clean_name(context.active_object.name) if context.active_object else "NewPrefab"
            self.filepath = layout.resolve(f"prefabs/{name}.prefab")
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        path = os.path.abspath(bpy.path.abspath(self.filepath))
        if not path.endswith(".prefab"):
            path += ".prefab"
        layout = project.locate(path)
        if layout is None:
            self.report({"ERROR"}, "Choose a .prefab path under the game's assets/ directory.")
            return {"CANCELLED"}

        written = False
        try:
            members = geometry.selection(context)
            target = geometry_prefab.prepare(path, layout)
            if host.resolve_cli_command(layout.root) is None:
                raise new_prefab.CreateError("Install the Paradise CLI or set its path in addon preferences.")

            with tempfile.TemporaryDirectory(prefix="paradise-geometry-") as temporary:
                staged = os.path.join(temporary, "selection.glb")
                geometry.export(context, members, staged)
                blocked = watch.ensure(layout.root)
                if blocked:
                    raise new_prefab.CreateError(blocked)
                geometry_prefab.prepare(path, layout)
                os.makedirs(os.path.dirname(target.model), exist_ok=True)
                # Publish a complete GLB so the watcher cannot observe a partial export. A hard
                # link refuses a target another author created while Blender was exporting.
                with tempfile.NamedTemporaryFile(
                    dir=os.path.dirname(target.model), suffix=".tmp", delete_on_close=False,
                ) as pending:
                    with open(staged, "rb") as source:
                        shutil.copyfileobj(source, pending)
                    pending.close()
                    os.link(pending.name, target.model)
                written = True

            new_prefab.identify(target.model, layout.relative(target.model))
            _geometry_cli(["assets", "extract", target.model], layout)
            with open(target.seed, encoding="utf-8") as handle:
                loads(handle.read(), target.seed)
            if os.path.normcase(target.seed) != os.path.normcase(target.prefab):
                new_prefab.refuse_target(target.prefab, layout)
                _geometry_cli(["assets", "mv", target.seed, target.prefab], layout)
            reference = new_prefab.identify(target.prefab, layout.relative(target.prefab))
        except (new_prefab.CreateError, PrefabDocumentError, OSError, ValueError, RuntimeError) as error:
            recovery = (
                f" The geometry is saved at {layout.relative(target.model)}; "
                "resolve the error and run paradise assets extract on that file."
                if written else ""
            )
            self.report({"ERROR"}, f"Could not create prefab: {str(error).rstrip('.')}.{recovery}")
            return {"CANCELLED"}

        self.report(
            {"INFO"},
            f"Saved {reference.path}. Use Add Prefab to place it, or Open Prefab to edit its components. "
            "The selected source meshes are unchanged.",
        )
        return {"FINISHED"}


def _geometry_cli(arguments: list[str], layout: project.ProjectLayout) -> None:
    result = host.run_cli([*arguments, "--project", layout.root], layout.root)
    if result is None:
        raise new_prefab.CreateError("The Paradise CLI could not be started.")
    if result.returncode != 0:
        raise new_prefab.CreateError((result.stderr or result.stdout).strip()[-1500:])


class PARADISE_ASSETS_OT_extract_prefab(Operator):
    """Move the active object and everything under it into a new prefab, leaving an instance"""

    bl_idname = "paradise_assets.extract_prefab"
    bl_label = "Extract to Prefab"
    # No UNDO: it writes two files and reloads the scene, and an undo step over that is a lie.
    bl_options = {"REGISTER"}

    filepath: StringProperty(subtype="FILE_PATH")  # type: ignore[valid-type]
    filter_glob: StringProperty(default="*.prefab", options={"HIDDEN"})  # type: ignore[valid-type]

    @classmethod
    def poll(cls, context):
        if store.read_state(context.scene) is None:
            return False
        obj = context.active_object
        return (
            obj is not None
            and store.guid_of(obj) is not None
            and not store.is_derived(obj)
            # The root IS the document; extracting it would leave an instance of everything.
            and obj.parent is not None
        )

    def invoke(self, context, event):
        state = store.read_state(context.scene)
        layout = project.locate(state.path) if state is not None else None
        if layout is not None and not self.filepath:
            name = store.document_name(context.active_object) or context.active_object.name
            self.filepath = os.path.join(layout.assets, "prefabs", f"{name}.prefab")
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        scene = context.scene
        state = store.read_state(scene)
        if state is None:
            self.report({"ERROR"}, "No prefab document is open.")
            return {"CANCELLED"}

        layout = project.locate(state.path)
        if layout is None:
            self.report({"ERROR"}, f"No asset project above {state.path}")
            return {"CANCELLED"}

        obj = context.active_object
        guid = store.guid_of(obj)
        if guid is None:
            self.report({"ERROR"}, "The active object is not a document object.")
            return {"CANCELLED"}

        path = os.path.abspath(bpy.path.abspath(self.filepath))
        if not path.endswith(".prefab"):
            path += ".prefab"

        # Before anything is written or saved, so a refused target costs nothing.
        try:
            relative = new_prefab.refuse_target(path, layout)
        except new_prefab.CreateError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        # The extraction works on the FILE, so anything Blender is still holding has to reach it
        # first -- and a reload at the end would drop pending edits without this.
        kept = workfile.unsaved_work(scene)
        if kept is not None:
            self.report(
                {"ERROR"},
                f"This scene has work the document does not: {kept}. Save to the prefab document "
                "first (Paradise Assets > Save), then extract.",
            )
            return {"CANCELLED"}

        # The new prefab's identity comes from the watcher's sidecar, and the instance left
        # behind cannot be written without it -- so no watcher means no extraction, and finding
        # that out before the level has been rewritten is the whole point of checking here.
        blocked = watch.ensure(layout.root)
        if blocked is not None:
            self.report({"ERROR"}, blocked)
            return {"CANCELLED"}

        try:
            save.save_prefab(scene)
            with open(state.path, encoding="utf-8") as handle:
                document = loads(handle.read(), state.path)
        except (save.SaveError, PrefabDocumentError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        except OSError as error:
            self.report({"ERROR"}, f"Could not read the document: {error}")
            return {"CANCELLED"}

        try:
            result = extract.extract(document, guid)
        except extract.ExtractError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        # Prefab first, then its identity, then the document that references it. The level is
        # untouched until there is something real to point it at.
        try:
            reference = new_prefab.create(path, layout, result.prefab)
        except (new_prefab.CreateError, OSError) as error:
            self.report({"ERROR"}, f"Could not create {relative}: {error}")
            return {"CANCELLED"}

        try:
            atomic.write_text(state.path, prefab_document.dumps(result.remaining(reference)))
        except OSError as error:
            self.report(
                {"ERROR"},
                f"{relative} was written but {os.path.basename(state.path)} could not be updated "
                f"({error}). Delete {relative} and its .meta, then try again.",
            )
            return {"CANCELLED"}

        with open(state.path, encoding="utf-8") as handle:
            remaining = loads(handle.read(), state.path)
        load.load_document(scene, remaining, state.path, layout)
        workfile.save(layout, state.path)

        for warning in result.warnings[:5]:
            self.report({"WARNING"}, warning)
        self.report(
            {"INFO"},
            f"Extracted {result.objects} object(s) into {relative}. "
            "Refresh the catalogue to see it in the Asset Browser.",
        )
        return {"FINISHED"}


#: The dropdown's items, kept alive on purpose: Blender stores no reference to the strings a
#: dynamic enum callback returns, so a list built inside the callback is freed and the menu
#: draws garbage.


#: The catalogue build, in its own Blender. ``__package__``, not a literal: an extension's
#: module is ``bl_ext.<repo>.paradise_assets``. The root travels as an argument after ``--``
#: rather than inside the expression, where a quote in the path would break the script.
_CATALOGUE_SCRIPT = (
    "import importlib, sys;"
    "c=importlib.import_module('{package}.catalogue');"
    "print('CATALOGUE', *c.build(sys.argv[sys.argv.index('--') + 1]))"
)


class PARADISE_ASSETS_OT_group_objects(Operator):
    """Put the selected document objects under a new group, which is an Empty they are parented to"""

    bl_idname = "paradise_assets.group_objects"
    bl_label = "Group Objects"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if store.read_state(context.scene) is None:
            return False
        selected = [obj for obj in context.selected_objects if store.guid_of(obj) is not None]
        return bool(selected) and all(
            obj.parent is not None and not store.is_derived(obj) for obj in selected)

    def execute(self, context):
        members = [obj for obj in context.selected_objects if store.guid_of(obj) is not None]
        try:
            group = grouping.group_objects(context.scene, members, context.active_object)
        except grouping.GroupError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        for obj in context.selected_objects:
            obj.select_set(False)
        group.select_set(True)
        context.view_layer.objects.active = group
        self.report({"INFO"}, f"Grouped {len(members)} object(s) under '{group.name}'. Save to keep it.")
        return {"FINISHED"}


class PARADISE_ASSETS_OT_refresh_catalogue(Operator):
    """Regenerate the Asset Browser catalogue of this project's prefabs"""

    bl_idname = "paradise_assets.refresh_catalogue"
    bl_label = "Refresh Prefab Catalogue"

    _process = None
    _timer = None
    _root = None

    @classmethod
    def poll(cls, context):
        return store.project_of(context.scene) is not None

    def execute(self, context):
        layout = store.project_of(context.scene)
        if layout is None:
            self.report({"ERROR"}, "No asset project found for this file")
            return {"CANCELLED"}
        self._root = layout.root

        # Own process: building the catalogue REPLACES the current file.
        argv = [
            bpy.app.binary_path, "--background",
            "--python-expr", _CATALOGUE_SCRIPT.format(package=__package__),
            "--", layout.root,
        ]
        if bpy.app.background or context.window is None:
            # A cold cache renders ~0.5 s per prefab, so a large project's first build is minutes.
            result = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
            return self._finished(result.returncode, result.stdout, result.stderr)

        # Polled from a timer: the build takes minutes and must not freeze the UI (#36).
        try:
            self._process = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                stdin=subprocess.DEVNULL,
            )
        except OSError as error:
            self.report({"ERROR"}, f"Could not start the catalogue build: {error}")
            return {"CANCELLED"}
        window_manager = context.window_manager
        self._timer = window_manager.event_timer_add(0.5, window=context.window)
        window_manager.modal_handler_add(self)
        self.report({"INFO"}, "Rebuilding the prefab catalogue in the background…")
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if self._process.poll() is None:
            return {"PASS_THROUGH"}
        self._drop_timer(context)
        stdout, stderr = self._process.communicate()
        return self._finished(self._process.returncode, stdout, stderr)

    def cancel(self, context) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
        self._drop_timer(context)

    def _drop_timer(self, context) -> None:
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None

    def _finished(self, returncode: int, stdout: str, stderr: str) -> set[str]:
        if returncode != 0:
            self.report({"ERROR"}, f"Catalogue build failed: {(stderr or '').strip()[-300:]}")
            return {"CANCELLED"}

        fields = next(
            (line.split() for line in (stdout or "").splitlines() if line.startswith("CATALOGUE")),
            [],
        )
        made = fields[1] if len(fields) > 1 else "?"
        pictured = fields[2] if len(fields) > 2 else "?"

        # Register HERE: a preferences change in the background Blender dies with it, and an
        # unregistered catalogue is a file nothing looks at (how it first shipped).
        name, added = catalogue.ensure_library(self._root)
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



def _instance_of(obj):
    """The instance ``obj`` belongs to: itself when it is one, else the one it was resolved out
    of. Clicking a wall of a building and asking to unpack means the building."""
    while obj is not None:
        if store.prefab_of(obj) is not None:
            return obj
        obj = obj.parent
    return None


class _InstanceOperator(Operator):
    """Shared shape for the gestures that rewrite a document around one instance.

    Each works on the FILE, exactly as extraction does: the scene is written first, the document
    re-read, the surgery done on what is actually on disk, and the result rematerialized. None of
    them needs the watcher -- no asset is created, so there is no identity to wait for, and
    borrowing extraction's ``watch.ensure`` would refuse the operation for a reason that is not
    true here.
    """

    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        if store.read_state(context.scene) is None:
            return False
        return _instance_of(context.active_object) is not None

    def _prepare(self, context):
        """``(scene state, layout, instance object)``, or ``None`` after reporting why not."""
        scene = context.scene
        state = store.read_state(scene)
        if state is None:
            self.report({"ERROR"}, "No prefab document is open.")
            return None

        layout = project.locate(state.path)
        if layout is None:
            self.report({"ERROR"}, f"No asset project above {state.path}")
            return None

        instance = _instance_of(context.active_object)
        if instance is None:
            self.report({"ERROR"}, "This object does not belong to a prefab instance.")
            return None

        kept = workfile.unsaved_work(scene)
        if kept is not None:
            self.report(
                {"ERROR"},
                f"This scene has work the document does not: {kept}. Save to the prefab document "
                "first (Paradise Assets > Save), then try again.",
            )
            return None

        try:
            save.save_prefab(scene)
        except save.SaveError as error:
            self.report({"ERROR"}, str(error))
            return None
        return state, layout, instance

    def _rematerialize(self, context, state, layout):
        with open(state.path, encoding="utf-8") as handle:
            document = loads(handle.read(), state.path)
        load.load_document(context.scene, document, state.path, layout)
        workfile.save(layout, state.path)

    def _prefabs(self, layout):
        def read(reference):
            try:
                with open(layout.resolve(reference.path), encoding="utf-8") as handle:
                    return loads(handle.read(), reference.path)
            except (OSError, PrefabDocumentError):
                return None
        return read


class PARADISE_ASSETS_OT_unpack_instance(_InstanceOperator):
    """Break this prefab instance's link, keeping the objects it was showing"""

    bl_idname = "paradise_assets.unpack_instance"
    bl_label = "Unpack Prefab Instance"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        prepared = self._prepare(context)
        if prepared is None:
            return {"CANCELLED"}
        state, layout, instance = prepared
        guid = store.guid_of(instance)

        try:
            with open(state.path, encoding="utf-8") as handle:
                document = loads(handle.read(), state.path)
            result = unpack.unpack(document, guid, self._prefabs(layout))
        except (unpack.UnpackError, PrefabDocumentError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        except OSError as error:
            self.report({"ERROR"}, f"Could not read the document: {error}")
            return {"CANCELLED"}

        try:
            atomic.write_text(state.path, prefab_document.dumps(result.document))
        except OSError as error:
            self.report({"ERROR"}, f"Could not write the document: {error}")
            return {"CANCELLED"}

        self._rematerialize(context, state, layout)
        for warning in result.warnings[:5]:
            self.report({"WARNING"}, warning)
        self.report(
            {"INFO"},
            f"Unpacked {result.objects} object(s); this document now owns them. "
            "They keep the identities they had, so nothing that referenced them has broken.",
        )
        return {"FINISHED"}


class PARADISE_ASSETS_OT_apply_overrides(_InstanceOperator):
    """Write this instance's overrides into the prefab, changing every instance of it"""

    bl_idname = "paradise_assets.apply_overrides"
    bl_label = "Apply Overrides to Prefab"

    @classmethod
    def poll(cls, context):
        return super().poll(context) and _has_overrides(_instance_of(context.active_object))

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        prepared = self._prepare(context)
        if prepared is None:
            return {"CANCELLED"}
        state, layout, instance = prepared
        guid = store.guid_of(instance)

        reference = store.prefab_of(instance)
        prefab_path = layout.resolve(reference[1])
        try:
            with open(state.path, encoding="utf-8") as handle:
                document = loads(handle.read(), state.path)
            with open(prefab_path, encoding="utf-8") as handle:
                target = loads(handle.read(), prefab_path)
            result = apply_overrides.apply_to_prefab(document, guid, target)
            result.prefab.validate(prefab_path)
        except (apply_overrides.ApplyError, PrefabDocumentError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        except OSError as error:
            self.report({"ERROR"}, f"Could not read {reference[1]}: {error}")
            return {"CANCELLED"}

        if not result.applied:
            self.report({"INFO"}, "This instance overrides nothing; the prefab is unchanged.")
            return {"CANCELLED"}

        # The PREFAB first. If the level write then fails the value is applied twice -- the
        # override and the prefab agree, which looks identical and reloads correctly. The other
        # order loses the overrides outright.
        try:
            atomic.write_text(prefab_path, prefab_document.dumps(result.prefab))
        except OSError as error:
            self.report({"ERROR"}, f"Could not write {reference[1]}: {error}")
            return {"CANCELLED"}

        try:
            atomic.write_text(state.path, prefab_document.dumps(result.remaining))
        except OSError as error:
            self.report(
                {"ERROR"},
                f"{reference[1]} was updated but {os.path.basename(state.path)} could not be "
                f"({error}). Reload: the overrides are still on the instance and now agree with "
                "the prefab, so nothing is lost.",
            )
            return {"CANCELLED"}

        self._rematerialize(context, state, layout)
        for warning in result.warnings[:5]:
            self.report({"WARNING"}, warning)
        self.report(
            {"INFO"},
            f"Applied {result.components} component(s) and {result.children} child change(s) to "
            f"{reference[1]}.",
        )
        return {"FINISHED"}


class PARADISE_ASSETS_OT_revert_instance(_InstanceOperator):
    """Throw away this instance's overrides, so it shows exactly what its prefab says"""

    bl_idname = "paradise_assets.revert_instance"
    bl_label = "Revert Instance to Prefab"

    @classmethod
    def poll(cls, context):
        return super().poll(context) and _has_overrides(_instance_of(context.active_object))

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        prepared = self._prepare(context)
        if prepared is None:
            return {"CANCELLED"}
        state, layout, instance = prepared
        guid = store.guid_of(instance)

        try:
            with open(state.path, encoding="utf-8") as handle:
                document = loads(handle.read(), state.path)
            reverted = overrides.strip_instance(document, guid)
            atomic.write_text(state.path, prefab_document.dumps(reverted))
        except PrefabDocumentError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        except OSError as error:
            self.report({"ERROR"}, f"Could not rewrite the document: {error}")
            return {"CANCELLED"}

        self._rematerialize(context, state, layout)
        self.report({"INFO"}, "The instance shows its prefab again; its overrides are gone.")
        return {"FINISHED"}


def _has_overrides(instance) -> bool:
    """Whether this instance's own entry, or one of its children, says anything of its own.

    Read from the .blend's tags rather than the file, because a poll runs at menu-draw rate and
    must not open a document.
    """
    if instance is None:
        return False
    from .materialize import tagging
    if tagging.overridden(instance):
        return True
    guid = store.guid_of(instance)
    for obj in instance.children_recursive:
        address = store.local_of(obj)
        if address is not None and address[0] == guid and tagging.overridden(obj):
            return True
    return False


classes = (
    PARADISE_ASSETS_OT_open_prefab,
    PARADISE_ASSETS_OT_reload_prefab,
    PARADISE_ASSETS_OT_recreate_workfile,
    PARADISE_ASSETS_OT_save_prefab,
    PARADISE_ASSETS_OT_create_prefab,
    PARADISE_ASSETS_OT_toggle_watch,
    PARADISE_ASSETS_OT_add_prefab_instance,
    PARADISE_ASSETS_OT_extract_prefab,
    PARADISE_ASSETS_OT_unpack_instance,
    PARADISE_ASSETS_OT_apply_overrides,
    PARADISE_ASSETS_OT_revert_instance,
    PARADISE_ASSETS_OT_group_objects,
    PARADISE_ASSETS_OT_refresh_catalogue,
    PARADISE_ASSETS_FH_prefab,
)
