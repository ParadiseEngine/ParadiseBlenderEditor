"""The "Paradise Assets" sidebar tab.

Two panels: what document this scene came from and what can be done to it, and what the selected
object is in that document.

The second used to be READ-ONLY, on the grounds that component payloads pass through untouched
and a panel that let you type into them would promise an edit that never reached the file. Both
halves of that are still true -- the save still takes payloads from the re-read document -- and
what changed is that an edit is now recorded as an OVERLAY of the fields an author touched
(see ..edits), applied over the file version at save. A component nobody edited is still written
back byte-for-byte, including one this addon has never heard of.

A field is editable when the GAME schema describes it and the host does not author it. Everything
else is shown as it always was: the format own meta and transform (Blender name field and gizmo
are their editor), nested payloads, and anything a dump does not mention.
"""

from __future__ import annotations

import os

import bpy
from bpy.types import Panel

from . import component_ops, edits, watch
from .document import project
from .materialize import store, sync

__all__ = ["classes"]


class _AssetsPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Paradise Assets"


class PARADISE_ASSETS_PT_document(_AssetsPanel, Panel):
    bl_label = "Prefab Document"
    bl_idname = "PARADISE_ASSETS_PT_document"

    def draw(self, context):
        layout = self.layout
        state = store.read_state(context.scene)

        if state is None:
            layout.label(text="No prefab document loaded.", icon="INFO")
            layout.operator("paradise_assets.open_prefab", icon="FILE_FOLDER")
            return

        box = layout.box()
        box.label(text=os.path.basename(state.path), icon="FILE_TEXT")
        box.label(text=os.path.dirname(state.path))

        count = sum(1 for obj in context.scene.collection.all_objects if store.guid_of(obj))
        box.label(text=f"{count} document object(s)")

        if state.is_stale:
            warning = layout.box()
            warning.alert = True
            warning.label(text="Changed on disk since it was opened.", icon="ERROR")
            warning.label(text="Reload, or your save will be refused.")

        # In the PAST tense, and separate from the warning above, which only predicts a refusal.
        # Saving writes the document too, and a handler can neither open a dialog nor cancel the
        # save -- so a refusal that is not said here is a save the author believes happened.
        refused = sync.refusal(context.scene)
        if refused is not None:
            box = layout.box()
            box.alert = True
            box.label(text="The last save did NOT reach the document.", icon="ERROR")
            box.label(text="Your work is in the working file, not lost.")
            box.label(text=refused[:70])

        # Above save/reload, and on its own: it is the one button here that ADDS something, and
        # the drag-and-drop route is not discoverable from a panel.
        layout.operator("paradise_assets.add_prefab_instance", text="Add Prefab…", icon="ADD")
        layout.operator("paradise_assets.refresh_catalogue", icon="ASSET_MANAGER")

        column = layout.column(align=True)
        column.operator("paradise_assets.save_prefab", icon="EXPORT")
        column.operator("paradise_assets.reload_prefab", icon="FILE_REFRESH")
        layout.operator("paradise_assets.open_prefab", text="Open Another…", icon="FILE_FOLDER")


class PARADISE_ASSETS_PT_play(_AssetsPanel, Panel):
    """Build the project and run the game on whatever document is open."""

    bl_label = "Play"
    bl_idname = "PARADISE_ASSETS_PT_play"
    bl_parent_id = "PARADISE_ASSETS_PT_document"

    @classmethod
    def poll(cls, context):
        return store.read_state(context.scene) is not None

    def draw(self, context):
        layout = self.layout

        # Resolved on every redraw, which is why `status` neither logs nor touches the project:
        # an author with nothing configured would otherwise get a warning per frame.
        from .play.ops import status

        problems = status()
        if problems:
            box = layout.box()
            box.alert = True
            for icon, message in problems:
                box.label(text=message, icon=icon)

        layout.operator("paradise_assets.play", icon="PLAY")

        # The watcher has no console anyone is looking at, so this row is where a failed rebuild
        # surfaces at all. Until a tray icon exists (ParadiseEngine#192) it is the only place.
        _draw_watch(layout, context)

        row = layout.row(align=True)
        row.operator("paradise_assets.build", icon="MOD_BUILD")
        row.operator("paradise_assets.verify", icon="CHECKMARK")
        # Off on its own: the only button here that deletes anything.
        layout.operator("paradise_assets.clean", icon="TRASH")


def _draw_watch(layout, context) -> None:
    """Whether a watcher is running for this project, and the last thing it complained about."""
    state = store.read_state(context.scene)
    if state is None:
        return
    # locate can fail even with a document open -- the project may have been moved or deleted out
    # from under the session -- and a panel that raised would take the whole sidebar with it.
    layout_ = project.locate(state.path)
    if layout_ is None:
        return
    root = layout_.root

    running = watch.is_running(root)
    row = layout.row(align=True)
    row.label(
        text="Watching assets" if running else "Not watching",
        icon="RADIOBUT_ON" if running else "RADIOBUT_OFF")
    row.operator(
        "paradise_assets.toggle_watch",
        text="Stop" if running else "Start",
        icon="PAUSE" if running else "PLAY")

    if running and (problem := watch.last_error(root)) is not None:
        box = layout.box()
        box.alert = True
        box.label(text="Last rebuild reported:", icon="ERROR")
        box.label(text=problem[:70])
    elif not running and (reason := watch.exit_reason(root)) is not None:
        # A watcher that started and then STOPPED used to be indistinguishable here from one that
        # was never started, which is the difference between "click Start" and "something is
        # wrong". Whatever it said on the way out is the only clue an author has.
        box = layout.box()
        box.alert = True
        box.label(text="The watcher stopped on its own.", icon="ERROR")
        for line in _wrap(reason, 44)[:3]:
            box.label(text=line)


class PARADISE_ASSETS_PT_object(_AssetsPanel, Panel):
    bl_label = "Components"
    bl_idname = "PARADISE_ASSETS_PT_object"
    bl_parent_id = "PARADISE_ASSETS_PT_document"

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def draw(self, context):
        layout = self.layout
        obj = context.active_object

        guid = store.guid_of(obj)
        if guid is None:
            layout.label(text="Not a document object.", icon="DOT")
            return

        layout.label(text=guid, icon="COPY_ID")

        components = store.component_json(obj)
        if not components:
            layout.label(text="No components.")
            return

        vocabulary = component_ops.vocabulary_for(context)
        pending = edits.read(obj)

        if pending:
            row = layout.box().row()
            row.label(text=f"{edits.count(obj)} unsaved field edit(s)", icon="GREASEPENCIL")
            row.operator("paradise_assets.revert_component_field", text="", icon="LOOP_BACK").component_id = ""

        if not vocabulary:
            # Not a failure: the dump is a build product of the GAME, and a fresh clone has none.
            # Saying which file is missing is the difference between "this addon is broken" and
            # "build the launcher once".
            layout.label(text="No schema — build the game to edit fields.", icon="INFO")

        for component in components:
            component_id = str(component.get("id", ""))
            schema = vocabulary.get(component_id)
            edited = pending.get(component_id, {})

            box = layout.box()
            header = box.row()
            header.label(text=_component_label(component), icon="PROPERTIES")
            if edited:
                revert = header.operator(
                    "paradise_assets.revert_component_field", text="", icon="LOOP_BACK")
                revert.component_id = component_id
                revert.field_name = ""

            if schema is None:
                # Either the format's own two (meta, transform -- Blender's name field and gizmo
                # ARE their editor) or a component this game's dump does not describe. Shown as
                # it was before, because a value nobody can type is still a value worth reading.
                for line in _payload_lines(component.get("data")):
                    box.label(text=line)
                continue

            _draw_schema_fields(box, component, schema, edited)


def _draw_schema_fields(box, component: dict, schema, edited: dict) -> None:
    """One component's fields, editable where the schema says they can be."""
    data = component.get("data")
    data = data if isinstance(data, dict) else {}

    for field in schema.fields:
        row = box.row(align=True)
        value = edited.get(field.name, data.get(field.name, field.default))

        if not field.editable:
            # A host-authored field is shown and not offered: its value comes from the object it
            # points at, so typing one in would be authoring in the place the export overwrites.
            row.label(text=f"{field.name}: {_short_value(value)}", icon="DECORATE_LOCKED")
            continue

        label = f"{field.name}: {_short_value(value)}"
        edit = row.operator(
            "paradise_assets.edit_component_field",
            text=label,
            icon="GREASEPENCIL" if field.name in edited else "DOT")
        edit.component_id = str(component.get("id", ""))
        edit.field_name = field.name

        if field.name in edited:
            revert = row.operator(
                "paradise_assets.revert_component_field", text="", icon="LOOP_BACK")
            revert.component_id = str(component.get("id", ""))
            revert.field_name = field.name


def _short_value(value) -> str:
    """A value as one short line. A panel row is not a document viewer."""
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
            return "(" + ", ".join(f"{float(v):g}" for v in value) + ")"
        return f"[{len(value)} item(s)]"
    if isinstance(value, dict):
        # An asset reference is the dict an author actually recognises, so it reads as its path.
        path = value.get("path")
        return str(path) if isinstance(path, str) else "{…}"
    if value is None:
        return "—"
    text = str(value)
    return text if len(text) <= 28 else text[:27] + "…"


def _component_label(component: dict) -> str:
    """The CLR type name where there is one, and the id otherwise.

    The type is the readable half and what an author recognises; the id is the primary key and
    the only thing guaranteed present.
    """
    type_name = component.get("type")
    if isinstance(type_name, str) and type_name:
        return type_name.rsplit(".", 1)[-1]
    return str(component.get("id", "<unknown>"))


def _payload_lines(data, prefix: str = "", depth: int = 0) -> list[str]:
    """A payload flattened to one line per leaf.

    Depth-limited because a panel is not a document viewer: a deeply nested payload would push
    everything else off the screen, and the file is one click away for anyone who needs the rest.
    """
    if not isinstance(data, dict) or depth > 2:
        return []

    lines: list[str] = []
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            lines.extend(_payload_lines(value, f"{path}.", depth + 1))
        elif isinstance(value, list):
            lines.append(f"{path}: [{len(value)} item(s)]")
        else:
            lines.append(f"{path}: {value}")
    return lines


classes = (
    PARADISE_ASSETS_PT_document,
    # Child panels follow their parent: Blender warns about an unregistered bl_parent_id
    # otherwise, and unregistration walks this in reverse for the same reason.
    PARADISE_ASSETS_PT_play,
    PARADISE_ASSETS_PT_object,
)


def _wrap(text: str, width: int) -> list[str]:
    """Panel labels do not wrap, so a message longer than a row has to be broken by hand."""
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines
