"""Project-scoped document editing, independent of the open prefab and active object."""

from __future__ import annotations

import copy
import json

from bpy.props import EnumProperty, IntProperty, StringProperty
from bpy.types import Operator, Panel

from . import edits, field_widgets
from .document import authoring_documents, component_schema
from .materialize import store

ID_KEY = "paradise_document_id"
ROOT_KEY = "paradise_document_root"
DATA_KEY = "paradise_document_data"
_DOCUMENT_ITEMS: list = []


def current(context):
    owner = context.window_manager
    layout = store.project_of(context.scene)
    if layout is None or owner.get(ROOT_KEY) != layout.root:
        return None
    ident = owner.get(ID_KEY)
    declaration = next((doc for doc in authoring_documents.load(layout.root) if doc.id == ident), None)
    if declaration is None:
        return None
    schema = declaration.schema(component_schema.load(layout.root))
    data = json.loads(owner.get(DATA_KEY, "{}"))
    return layout, declaration, schema, data


def open_document(context, ident: str) -> None:
    layout = store.project_of(context.scene)
    if layout is None:
        raise ValueError("Open a project workfile before editing project settings")
    document = next((doc for doc in authoring_documents.load(layout.root) if doc.id == ident), None)
    if document is None:
        raise ValueError(f"The game does not declare project document {ident!r}")
    document.schema(component_schema.load(layout.root))
    payload = authoring_documents.read(layout, document)
    owner = context.window_manager
    owner[ROOT_KEY] = layout.root
    owner[ID_KEY] = ident
    owner[DATA_KEY] = json.dumps(payload, sort_keys=True)
    edits.clear(owner)


def merged(context) -> dict:
    state = current(context)
    if state is None:
        return {}
    _, document, _, data = state
    for path, value in edits.edited_fields(context.window_manager, document.id).items():
        edits.write_path(data, path, copy.deepcopy(value))
    return data


def save(context) -> None:
    state = current(context)
    if state is None:
        raise ValueError("Choose a project settings document first")
    layout, document, schema, baseline = state
    owner = context.window_manager
    payload = authoring_documents.save(
        layout, document, schema, edits.edited_fields(owner, document.id), baseline=baseline
    )
    owner[DATA_KEY] = json.dumps(payload, sort_keys=True)
    edits.clear(owner)


def _document_items(self, context):
    global _DOCUMENT_ITEMS
    layout = store.project_of(context.scene) if context else None
    try:
        documents = authoring_documents.load(layout.root) if layout else ()
        _DOCUMENT_ITEMS = [(doc.id, doc.display_name, doc.path) for doc in documents]
    except (OSError, ValueError):
        _DOCUMENT_ITEMS = []
    return _DOCUMENT_ITEMS


class PARADISE_ASSETS_OT_open_project_settings(Operator):
    """Choose a game-declared settings document; save or reload pending changes first"""

    bl_idname = "paradise_assets.open_project_settings"
    bl_label = "Choose Project Settings"
    bl_property = "document"
    document: EnumProperty(items=_document_items)

    def invoke(self, context, _event):
        context.window_manager.invoke_search_popup(self)
        return {"FINISHED"}

    def execute(self, context):
        if edits.count(context.window_manager):
            self.report({"ERROR"}, "Save or reload the current settings before choosing another document")
            return {"CANCELLED"}
        try:
            open_document(context, self.document)
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class PARADISE_ASSETS_OT_save_project_settings(Operator):
    """Save edited settings, retaining unrelated values currently on disk"""

    bl_idname = "paradise_assets.save_project_settings"
    bl_label = "Save Settings"

    def execute(self, context):
        try:
            save(context)
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, "Project settings saved")
        return {"FINISHED"}


class PARADISE_ASSETS_OT_reload_project_settings(Operator):
    """Discard pending settings edits and reread the document"""

    bl_idname = "paradise_assets.reload_project_settings"
    bl_label = "Reload Settings"

    def execute(self, context):
        try:
            open_document(context, context.window_manager.get(ID_KEY, ""))
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class PARADISE_ASSETS_OT_discard_project_settings(Operator):
    """Discard settings edits retained from another project or a removed document"""

    bl_idname = "paradise_assets.discard_project_settings"
    bl_label = "Discard Pending Settings"

    def execute(self, context):
        owner = context.window_manager
        edits.clear(owner)
        for key in (ID_KEY, ROOT_KEY, DATA_KEY):
            if key in owner:
                del owner[key]
        return {"FINISHED"}


class PARADISE_ASSETS_OT_edit_project_structure(Operator):
    """Edit a list or an optional field in the selected settings document"""

    bl_idname = "paradise_assets.edit_project_structure"
    bl_label = "Edit Settings Field"
    action: EnumProperty(
        items=[(value, value.title(), "") for value in ("add", "remove", "set", "unset", "revert")]
    )
    field_name: StringProperty()
    index: IntProperty(min=0)

    def execute(self, context):
        try:
            state = current(context)
            if state is None:
                raise ValueError("Choose a project settings document first")
            _, document, schema, _ = state
            field = schema.resolve(self.field_name)
            if field is None:
                raise ValueError("The field is no longer in the schema; reload settings")
            owner = context.window_manager
            if self.action == "revert":
                pending = edits.edited_fields(owner, document.id)
                if any(self.field_name.startswith(key + "/") for key in pending):
                    raise ValueError("Revert the containing list or optional object as a whole")
                for key in pending:
                    if key == self.field_name or key.startswith(self.field_name + "/"):
                        edits.clear(owner, document.id, key)
                return {"FINISHED"}
            if self.action in ("set", "unset"):
                if not field.optional:
                    raise ValueError("Only optional fields can be set or omitted")
                value = field.default_value() if self.action == "set" else None
            else:
                if field.type != "array" or field.items is None:
                    raise ValueError("The field is not an editable list")
                value = copy.deepcopy(edits.read_path(merged(context), self.field_name) or [])
                if self.action == "add":
                    value.append(copy.deepcopy(field.items.default_value()))
                elif self.index < len(value):
                    del value[self.index]
                else:
                    raise ValueError("The list row no longer exists")
            edits.set_field(owner, document.id, self.field_name, value)
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


def draw_fields(layout, context) -> None:
    state = current(context)
    if state is None:
        return
    _, document, schema, _ = state
    owner = context.window_manager
    data = merged(context)
    plan = schema.plan(data)
    edited = edits.edited_fields(owner, document.id)
    rows = [
        (document.id, item, edits.read_path(data, item.path))
        for item in plan
        if item.role in (component_schema.ROLE_LEAF, component_schema.ROLE_ROW)
        and item.field.editable
        and not component_schema.is_asset_field(item.field)
    ]
    field_widgets.sync(context, owner, rows, scope="document")
    for item in plan:
        value = edits.read_path(data, item.path)
        if item.role == component_schema.ROLE_OPTIONAL:
            row = layout.row(align=True)
            row.label(text=item.path + (" (unset)" if value is None else ""))
            op = row.operator(
                "paradise_assets.edit_project_structure",
                text="Set" if value is None else "Clear",
                icon="ADD" if value is None else "X",
            )
            op.action = "set" if value is None else "unset"
            op.field_name = item.path
            field_widgets._draw_revert(row, edited, document.id, item.path, "document")
        elif item.role == component_schema.ROLE_ARRAY:
            row = layout.row(align=True)
            row.label(text=f"{item.path} ({len(value) if isinstance(value, list) else 0})")
            op = row.operator("paradise_assets.edit_project_structure", text="", icon="ADD")
            op.action, op.field_name = "add", item.path
            field_widgets._draw_revert(row, edited, document.id, item.path, "document")
        elif item.role == component_schema.ROLE_ROW:
            row = layout.row(align=True)
            if item.field.editable and not item.field.fields:
                field_widgets.draw_item(
                    layout, context, owner, document.id, item, value, edited, row=row, scope="document"
                )
            else:
                row.label(text=item.path)
            op = row.operator("paradise_assets.edit_project_structure", text="", icon="X")
            op.action, op.field_name, op.index = "remove", item.path.rpartition("/")[0], item.index or 0
        elif item.role == component_schema.ROLE_LEAF:
            field_widgets.draw_item(
                layout, context, owner, document.id, item, value, edited, scope="document"
            )
        elif item.role != component_schema.ROLE_OPTIONAL:
            layout.label(text=f"{item.path}: {component_schema.format_value(value)}", icon="LOCKED")


class PARADISE_ASSETS_PT_project_settings(Panel):
    bl_label = "Project Settings"
    bl_idname = "PARADISE_ASSETS_PT_project_settings"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Paradise"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return store.project_of(context.scene) is not None

    def draw(self, context):
        layout = self.layout
        located = store.project_of(context.scene)
        try:
            documents = authoring_documents.load(located.root)
            state = current(context)
            if state is None and edits.count(context.window_manager):
                layout.label(text="Unsaved settings remain in the previous document.", icon="ERROR")
                layout.label(text="Return to its project to save, or discard them.")
                layout.operator("paradise_assets.discard_project_settings", icon="TRASH")
            if not documents:
                layout.label(text="Build the game to expose project settings.", icon="INFO")
                layout.operator("paradise_assets.build_schema", icon="MOD_BUILD")
                return
            layout.operator(
                "paradise_assets.open_project_settings",
                icon="FILE_FOLDER",
                text=state[1].display_name if state else "Choose Settings Document",
            )
            if state is None:
                return
            layout.label(text=state[1].path)
            row = layout.row(align=True)
            row.operator("paradise_assets.save_project_settings", icon="FILE_TICK")
            row.operator("paradise_assets.reload_project_settings", icon="FILE_REFRESH")
            pending = edits.count(context.window_manager)
            if pending:
                layout.label(text=f"{pending} unsaved edit(s)", icon="GREASEPENCIL")
            draw_fields(layout, context)
        except (OSError, ValueError) as error:
            layout.label(text=str(error), icon="ERROR")


classes = (
    PARADISE_ASSETS_OT_open_project_settings,
    PARADISE_ASSETS_OT_save_project_settings,
    PARADISE_ASSETS_OT_reload_project_settings,
    PARADISE_ASSETS_OT_discard_project_settings,
    PARADISE_ASSETS_OT_edit_project_structure,
    PARADISE_ASSETS_PT_project_settings,
)
