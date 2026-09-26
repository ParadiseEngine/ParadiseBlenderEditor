"""The right-click entries for a document object, in both editors an author selects one in:
open the prefab it instantiates, turn it into one, group the selection under a new Empty, give a
placement a mesh of its own, open the ``.blend`` its model comes from, and -- for anything that
belongs to an instance -- apply, revert or break its overrides.

Both are reachable from the sidebar already. The menus are where an author's hand already is
when the question comes up -- the Outliner because it is the only place the document's tree is
legible, the viewport because that is where the object was clicked.

Opening a prefab starts a SECOND Blender rather than replacing this session, which is the whole
point of the entry: a level and the prop it instances are two documents, and editing the prop
by closing the level is what makes people not do it. The two sessions share the project, and the
watcher already reconciles what either writes.
"""

from __future__ import annotations

import os
import subprocess

import bpy
from bpy.types import Operator

from .document import model_source
from .materialize import store
from .materialize.meshes import SOURCE_KEY

__all__ = ["classes", "register_menu", "unregister_menu"]

#: The menus the entries are appended to: the Outliner's object rows, and the viewport's object
#: context menu. One ``_draw`` for both -- they are handed the same active object, and an entry
#: that appeared in one place and not the other would read as a bug in whichever lacked it.
MENUS = ("OUTLINER_MT_object", "VIEW3D_MT_object_context_menu")
EDIT_MODE_MENU = "VIEW3D_MT_edit_mesh_context_menu"


def prefab_of(obj) -> tuple[str, str] | None:
    """The prefab ``obj`` came from, looking up through its parents.

    An instance's resolved children are ordinary objects carrying nothing of their own, so
    clicking a wall of a building instance has to find the building. The walk stops at the first
    reference: a prefab inside a prefab is answered by the innermost one, which is the one whose
    document holds what was clicked.
    """
    while obj is not None:
        if (reference := store.prefab_of(obj)) is not None:
            return reference
        obj = obj.parent
    return None


class PARADISE_ASSETS_OT_open_prefab_elsewhere(Operator):
    """Open the prefab this object instantiates in a new Blender, leaving this one alone"""

    bl_idname = "paradise_assets.open_prefab_elsewhere"
    bl_label = "Open Prefab in New Blender"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context) -> bool:
        obj = context.active_object
        return obj is not None and prefab_of(obj) is not None

    def execute(self, context):
        obj = context.active_object
        reference = prefab_of(obj) if obj is not None else None
        if reference is None:
            self.report({"ERROR"}, "This object does not instantiate a prefab")
            return {"CANCELLED"}

        layout = store.project_of(context.scene)
        if layout is None:
            self.report({"ERROR"}, "No asset project for this file")
            return {"CANCELLED"}

        _guid, relative = reference
        path = layout.resolve(relative)
        if not os.path.isfile(path):
            # The guid still names it, but this addon only reads sidecars and cannot search the
            # tree by identity; `paradise assets verify --fix` is what repoints a stale path.
            self.report(
                {"ERROR"},
                f"{relative} is referenced but not on disk. Run `paradise assets verify --fix` "
                "if it was moved.",
            )
            return {"CANCELLED"}

        problem = launch(path, layout.root)
        if problem is not None:
            self.report({"ERROR"}, problem)
            return {"CANCELLED"}

        self.report({"INFO"}, f"Opening {relative} in a new Blender…")
        return {"FINISHED"}


#: Run in the new Blender once it is up. `--` puts the path in ``sys.argv`` rather than inside
#: the expression, where a quote or a backslash in it would break the parse. EXEC_DEFAULT: the
#: operator's own invoke would put a file browser in front of a file we have already chosen.
_OPEN_SCRIPT = (
    "import bpy, sys;"
    "bpy.ops.paradise_assets.open_prefab("
    "'EXEC_DEFAULT', filepath=sys.argv[sys.argv.index('--') + 1])"
)


def launch(document_path: str, project_root: str) -> str | None:
    """Start a Blender on ``document_path``; a message on failure, ``None`` on success.

    NOT ``--factory-startup``: the new session needs this addon enabled, which means the user's
    preferences.
    """
    return _spawn([bpy.app.binary_path, "--python-expr", _OPEN_SCRIPT, "--", document_path], project_root)


def _spawn(argv: list[str], cwd: str) -> str | None:
    """Start ``argv`` detached (``start_new_session``) so it survives this Blender quitting -- the
    author opened a second editor, not a subprocess of the first. A message on failure."""
    # No console window on Windows; a GUI Blender would otherwise pop one behind the new session.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        subprocess.Popen(  # argv is built from resolved paths
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"Could not start Blender: {error}"
    return None


def _model_source_of(obj) -> str | None:
    """The model file the placement ``obj`` shows, as the library imported it."""
    collection = obj.instance_collection if obj is not None else None
    source = collection.get(SOURCE_KEY) if collection is not None else None
    return source if isinstance(source, str) else None


class PARADISE_ASSETS_OT_edit_model_source(Operator):
    """Open the .blend this object's model comes from in a new Blender. Saving it there
    re-extracts the model, and every placement shows the change on its next reload"""

    bl_idname = "paradise_assets.edit_model_source"
    bl_label = "Edit Source in New Blender"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context) -> bool:
        source = _model_source_of(context.active_object)
        if source is None or not model_source.is_converted(source):
            return False
        if not source.lower().endswith(".blend"):
            cls.poll_message_set(model_source.edit_in_place_refusal(source))
            return False
        return True

    def execute(self, context):
        source = _model_source_of(context.active_object)
        if source is None or not source.lower().endswith(".blend") or not os.path.isfile(source):
            self.report({"ERROR"}, "This object does not show a model made from a .blend on disk.")
            return {"CANCELLED"}
        # Opened as the main file with no script: the author edits the model itself, and the
        # asset watcher -- not this session -- picks up the save.
        problem = _spawn([bpy.app.binary_path, source], os.path.dirname(source))
        if problem is not None:
            self.report({"ERROR"}, problem)
            return {"CANCELLED"}
        self.report({"INFO"}, f"Opening {os.path.basename(source)} in a new Blender…")
        return {"FINISHED"}


def _draw(self, context) -> None:
    """Append our entries, and only for an object the addon has something to say about.

    A DOCUMENT object, not any object: both entries act on the document, so on a cube somebody
    just added they could only ever be greyed out, and a menu that grows two dead rows on every
    object is worse than one that says nothing. Between document objects the operators' own
    polls decide, which is Blender's convention — a greyed row says the entry exists.
    """
    obj = getattr(context, "active_object", None)
    if obj is not None and obj.type == "MESH" and store.guid_of(obj) is None:
        self.layout.separator()
        column = self.layout.column()
        column.operator_context = "INVOKE_DEFAULT"
        column.operator("paradise_assets.create_prefab", text="Create Prefab from Selection…", icon="EXPORT")
        return
    if obj is None or store.read_state(context.scene) is None or store.guid_of(obj) is None:
        return

    layout = self.layout
    layout.separator()
    column = layout.column()
    column.operator_context = "INVOKE_DEFAULT"
    column.operator(
        PARADISE_ASSETS_OT_open_prefab_elsewhere.bl_idname,
        text="Open Prefab in New Blender",
        icon="FILE_BLEND")
    column.operator(
        "paradise_assets.extract_prefab",
        text="Create Prefab from Object…",
        icon="EXPORT")
    column.operator(
        "paradise_assets.group_objects",
        text="Group Selected",
        icon="OUTLINER_COLLECTION")
    # Only on something that shows a model: on a group or a light it could only ever be greyed.
    # A prefab's child keeps the row, greyed, so its tooltip can say to unpack the instance.
    if obj.instance_collection is not None:
        column.operator(
            "paradise_assets.make_mesh_editable",
            text="Make Mesh Editable",
            icon="EDITMODE_HLT")
        # A converted model's GLB is derived, so it is edited where it comes from instead; an
        # FBX's row stays, greyed, so its tooltip can say where that is.
        if model_source.is_converted(_model_source_of(obj) or ""):
            column.operator(
                PARADISE_ASSETS_OT_edit_model_source.bl_idname,
                text="Edit Source in New Blender",
                icon="FILE_BLEND")
        else:
            column.operator(
                "paradise_assets.edit_shared_mesh",
                text="Edit Shared Mesh…",
                icon="LINKED")
    editing = store.editable_of(obj)
    if editing is not None and editing.shared:
        column.operator(
            "paradise_assets.finish_shared_mesh",
            text="Finish Editing Shared Mesh",
            icon="CHECKMARK")

    # Only for something that IS part of an instance: on a plain object these three could only
    # ever be greyed, and the menu already earns its rows.
    if prefab_of(obj) is None:
        return
    instances = layout.column()
    instances.operator_context = "INVOKE_DEFAULT"
    instances.separator()
    instances.operator(
        "paradise_assets.apply_overrides",
        text="Apply Overrides to Prefab…",
        icon="EXPORT")
    instances.operator(
        "paradise_assets.revert_instance",
        text="Revert Instance to Prefab",
        icon="LOOP_BACK")
    instances.operator(
        "paradise_assets.unpack_instance",
        text="Unpack Prefab Instance",
        icon="UNLINKED")


def _draw_edit_mode(self, context) -> None:
    """Edit Mode's right-click is a different menu, and it is where a shared-mesh edit ends."""
    obj = getattr(context, "active_object", None)
    editing = store.editable_of(obj) if obj is not None else None
    if editing is None or not editing.shared:
        return
    self.layout.separator()
    self.layout.operator(
        "paradise_assets.finish_shared_mesh",
        text="Finish Editing Shared Mesh",
        icon="CHECKMARK")


#: Menu -> what it appends, for the Object Mode menus and Edit Mode's.
_ENTRIES = (*((name, _draw) for name in MENUS), (EDIT_MODE_MENU, _draw_edit_mode))


def register_menu() -> None:
    """Append the entries to each menu that exists: a renamed bundled menu must not fail the
    whole addon's registration over a context-menu entry."""
    for name, draw in _ENTRIES:
        menu = getattr(bpy.types, name, None)
        if menu is not None:
            menu.append(draw)


def unregister_menu() -> None:
    for name, draw in _ENTRIES:
        menu = getattr(bpy.types, name, None)
        if menu is not None:
            menu.remove(draw)


classes = (PARADISE_ASSETS_OT_open_prefab_elsewhere, PARADISE_ASSETS_OT_edit_model_source)
