"""The right-click entries for a document object, in both editors an author selects one in:
open the prefab it instantiates, turn it into one, group the selection under a new Empty, and --
for anything that belongs to an instance -- apply, revert or break its overrides.

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

from .materialize import store

__all__ = ["classes", "register_menu", "unregister_menu"]

#: The menus the entries are appended to: the Outliner's object rows, and the viewport's object
#: context menu. One ``_draw`` for both -- they are handed the same active object, and an entry
#: that appeared in one place and not the other would read as a bug in whichever lacked it.
MENUS = ("OUTLINER_MT_object", "VIEW3D_MT_object_context_menu")


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
    preferences. Detached (``start_new_session``) so it survives this Blender quitting -- the
    author opened a second editor, not a subprocess of the first.
    """
    argv = [
        bpy.app.binary_path,
        "--python-expr", _OPEN_SCRIPT,
        "--", document_path,
    ]

    # No console window on Windows; a GUI Blender would otherwise pop one behind the new session.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        subprocess.Popen(  # argv is built from resolved paths
            argv,
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"Could not start Blender: {error}"
    return None


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


def register_menu() -> None:
    """Append the entries to each menu that exists: a renamed bundled menu must not fail the
    whole addon's registration over a context-menu entry."""
    for name in MENUS:
        menu = getattr(bpy.types, name, None)
        if menu is not None:
            menu.append(_draw)


def unregister_menu() -> None:
    for name in MENUS:
        menu = getattr(bpy.types, name, None)
        if menu is not None:
            menu.remove(_draw)


classes = (PARADISE_ASSETS_OT_open_prefab_elsewhere,)
