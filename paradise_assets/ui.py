"""The "Paradise Assets" sidebar tab. Edits go through the overlay in :mod:`edits`; ``meta``
and ``transform`` stay live from Blender, host-baked fields stay locked."""

from __future__ import annotations

import os

from bpy.types import Panel

from . import component_ops, edits, field_widgets, watch
from .document import component_schema, project, well_known
from .materialize import save, store, sync

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

        # A save_pre handler can neither open a dialog nor cancel the save, so a refusal not
        # said here is a save the author believes happened. The working file keeps the work:
        # a reopen does not rebuild a scene that carries a refusal (workfile.unsaved_work).
        refused = sync.refusal(context.scene)
        if refused is not None:
            box = layout.box()
            box.alert = True
            box.label(text="The last save did NOT reach the document.", icon="ERROR")
            box.label(text="Your work is in the working file, not lost.")
            box.label(text=refused[:70])

        row = layout.row(align=True)
        row.operator("paradise_assets.add_prefab_instance", text="Add Prefab…", icon="ADD")
        row.operator("paradise_assets.extract_prefab", text="Extract…", icon="EXPORT")
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

        # Every redraw: `status` must not log or a warning fires per frame.
        from .play.ops import status

        problems = status()
        if problems:
            box = layout.box()
            box.alert = True
            for icon, message in problems:
                box.label(text=message, icon=icon)

        layout.operator("paradise_assets.play", icon="PLAY")

        # The only place a failed rebuild surfaces until the tray (ParadiseEngine#192).
        _draw_watch(layout, context)

        row = layout.row(align=True)
        row.operator("paradise_assets.build", icon="MOD_BUILD")
        row.operator("paradise_assets.verify", icon="CHECKMARK")
        layout.operator("paradise_assets.clean", icon="TRASH")


def _draw_watch(layout, context) -> None:
    """Whether a watcher is running for this project, and the last thing it complained about."""
    state = store.read_state(context.scene)
    if state is None:
        return
    # A project moved out from under the session must not take the whole sidebar down.
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
        # A stopped watcher must not read as a never-started one.
        box = layout.box()
        box.alert = True
        box.label(text="The watcher stopped on its own.", icon="ERROR")
        for line in _wrap(reason, 44)[:3]:
            box.label(text=line)


class PARADISE_ASSETS_PT_models(_AssetsPanel, Panel):
    """One prefab per model, generated and kept in step."""

    bl_label = "Model Prefabs"
    bl_idname = "PARADISE_ASSETS_PT_models"
    bl_parent_id = "PARADISE_ASSETS_PT_document"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return store.read_state(context.scene) is not None

    def draw(self, context):
        from .prefs import get_preferences

        layout = self.layout
        state = store.read_state(context.scene)
        located = project.locate(state.path) if state is not None else None
        if located is None:
            return


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

        vocabulary = component_ops.vocabulary_for(context)
        pending = edits.read(obj)
        components = component_ops.components_of(obj)
        pending_count = edits.count(obj)

        if pending_count:
            row = layout.box().row()
            row.label(text=f"{pending_count} unsaved edit(s)", icon="GREASEPENCIL")
            row.operator(
                "paradise_assets.revert_component_field", text="", icon="LOOP_BACK"
            ).component_id = ""

        layout.operator("paradise_assets.add_component", icon="ADD")

        if not vocabulary:
            # Not a failure: a fresh clone has no dump until the launcher is built once, and a
            # `clean` that took .editor/ is back in that state. The button is that build.
            box = layout.box()
            box.label(text="No game schema — build the launcher to edit game fields.", icon="INFO")
            box.operator("paradise_assets.build_schema", icon="MOD_BUILD")

        if not components:
            layout.label(text="No components.")
            return

        widget_rows: list[tuple[str, object, object]] = []
        drawn: list[tuple] = []

        for component in components:
            component_id = str(component.get("id", ""))
            schema = vocabulary.describe(component)
            edited = pending.get(component_id, {})
            drawn.append((component, component_id, schema, edited))
            if schema is None or component_schema.is_format_owned(component_id):
                continue
            raw = component.get("data")
            data = raw if isinstance(raw, dict) else {}
            merged = component_ops.merged_data(obj, component_id, data)
            for item in schema.plan(merged):
                if item.role not in (
                    component_schema.ROLE_LEAF, component_schema.ROLE_ROW
                ):
                    continue
                if not item.field.editable:
                    continue
                value = edits.read_path(merged, item.path)
                if component_schema.is_asset_field(item.field, value):
                    continue
                if item.field.fields:
                    continue
                widget_rows.append((component_id, item, value))

        field_widgets.sync(context, obj, widget_rows)

        for component, component_id, schema, edited in drawn:
            box = layout.box()
            header = box.row()
            header.label(
                text=schema.display_name if schema is not None else _component_label(component),
                icon="PROPERTIES",
            )
            if edited:
                revert = header.operator(
                    "paradise_assets.revert_component_field", text="", icon="LOOP_BACK")
                revert.component_id = component_id
                revert.field_name = ""
            if not component_schema.is_format_owned(component_id):
                drop = header.operator(
                    "paradise_assets.remove_component", text="", icon="X")
                drop.component_id = component_id

            if component_schema.is_format_owned(component_id):
                if component_id.lower() == well_known.META_ID.lower():
                    _draw_meta(box, obj, component)
                else:
                    _draw_transform(box, obj)
                continue

            if schema is None:
                for line in _payload_lines(component.get("data")):
                    box.label(text=line)
                continue

            _draw_schema_fields(box, context, obj, component, schema, edited)


def _draw_schema_fields(box, context, obj, component: dict, schema, edited: dict) -> None:
    """One component's fields, editable where the schema says they can be."""
    raw = component.get("data")
    data = raw if isinstance(raw, dict) else {}
    component_id = str(component.get("id", ""))
    merged = component_ops.merged_data(obj, component_id, data)

    for item in schema.plan(merged):
        value = edits.read_path(merged, item.path)
        if item.role == component_schema.ROLE_LOCKED:
            row = box.row()
            row.label(
                text=f"{item.path}: {component_schema.format_value(value)}",
                icon="DECORATE_LOCKED",
            )
            continue

        if item.role == component_schema.ROLE_ARRAY:
            row = box.row(align=True)
            count = len(value) if isinstance(value, list) else 0
            row.label(text=item.path if count else f"{item.path}  (empty)")
            add = row.operator("paradise_assets.add_array_row", text="", icon="ADD")
            add.component_id = component_id
            add.field_name = item.path
            continue

        if item.role == component_schema.ROLE_ROW:
            row = box.row(align=True)
            row.label(text=str(item.index if item.index is not None else 0))
            array_path, _, _ = item.path.rpartition("/")
            if item.field.editable and (
                not item.field.fields or component_schema.is_asset_field(item.field, value)
            ):
                field_widgets.draw_item(
                    box, context, obj, component_id, item, value, edited, row=row)
            else:
                row.label(text=item.path, icon="DOT")
            drop = row.operator("paradise_assets.remove_array_row", text="", icon="X")
            drop.component_id = component_id
            drop.field_name = array_path
            drop.index = item.index if item.index is not None else 0
            continue

        field_widgets.draw_item(box, context, obj, component_id, item, value, edited)


def _draw_meta(box, obj, component: dict) -> None:
    """Identity, name and parent read LIVE from the object, since save writes ``obj.name`` and
    ``obj.parent`` and the snapshot lies the moment someone renames."""
    raw = component.get("data")
    data = raw if isinstance(raw, dict) else {}

    box.label(text=f"{well_known.GUID}: {store.guid_of(obj) or data.get(well_known.GUID) or '—'}",
              icon="DECORATE_LOCKED")
    box.label(text=f"{well_known.NAME}: {obj.name}", icon="DECORATE_LOCKED")

    parent = obj.parent
    parent_guid = store.guid_of(parent) if parent is not None else None
    row = box.row(align=True)
    row.label(text=f"{well_known.PARENT}:")
    if parent is not None and parent_guid:
        jump = row.operator(
            "paradise_assets.reveal_object", text=parent.name, icon="OBJECT_DATA")
        jump.guid = parent_guid
        box.label(text=parent_guid)
    elif parent is not None:
        row.label(text=f"{parent.name}  (not a document object)")
    else:
        row.label(text="— (root)")

    for key in (well_known.TARGET, well_known.DROPPED):
        if key in data:
            box.label(text=f"{key}: {component_schema.format_value(data.get(key))}",
                      icon="DECORATE_LOCKED")


def _draw_transform(box, obj) -> None:
    """Local TRS in document convention, live and read-only: the gizmo is the editor."""
    position, rotation, scale = save.document_trs(obj)
    box.label(text=f"{well_known.POSITION}: {component_schema.format_value(position)}",
              icon="DECORATE_LOCKED")
    box.label(text=f"{well_known.ROTATION}: {component_schema.format_value(rotation)}",
              icon="DECORATE_LOCKED")
    box.label(text=f"{well_known.SCALE}: {component_schema.format_value(scale)}",
              icon="DECORATE_LOCKED")


def _component_label(component: dict) -> str:
    """The CLR type name where there is one, else the id."""
    type_name = component.get("type")
    if isinstance(type_name, str) and type_name:
        return type_name.rsplit(".", 1)[-1]
    return str(component.get("id", "<unknown>"))


def _payload_lines(data, prefix: str = "", depth: int = 0) -> list[str]:
    """A payload as one line per leaf, depth-limited: a panel is not a document viewer."""
    if not isinstance(data, dict) or depth > 2:
        return []

    lines: list[str] = []
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            lines.extend(_payload_lines(value, f"{path}.", depth + 1))
        elif isinstance(value, list):
            lines.append(f"{path}: {component_schema.format_value(value)}")
        else:
            lines.append(f"{path}: {value}")
    return lines


classes = (
    PARADISE_ASSETS_PT_document,
    # Parents before children, or Blender warns about an unregistered bl_parent_id.
    PARADISE_ASSETS_PT_play,
    PARADISE_ASSETS_PT_models,
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
