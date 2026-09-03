"""Component operators: add/remove components and rows, revert edits, reveal objects.

Values must never be written back from an ID property: ID properties normalize types (``int``
-> ``float``, tuple -> list) and the document's numbers are a cross-language contract, so edits
go through :mod:`.edits` in the schema's declared shape.
"""

from __future__ import annotations

import copy

import bpy
from bpy.props import EnumProperty, IntProperty, StringProperty
from bpy.types import Operator

from . import edits
from .document import component_schema, project
from .materialize import store

__all__ = ["classes", "components_of", "merged_data", "schema_for", "vocabulary_for"]


def vocabulary_for(context) -> component_schema.Vocabulary:
    """The game's component vocabulary, asked for at draw time so a rebuild shows up without
    reopening; ``component_schema.load`` caches on the dump's stamp, so a redraw costs a stat."""
    state = store.read_state(context.scene)
    if state is None:
        return component_schema.Vocabulary({}, None)
    layout = project.locate(state.path)
    if layout is None:
        return component_schema.Vocabulary({}, None)
    return component_schema.load(layout.root)


def schema_for(context, obj, component_id: str):
    """The schema this object's component is edited through -- dump, engine form, or inferred."""
    vocabulary = vocabulary_for(context)
    for component in components_of(obj):
        if str(component.get("id", "")).lower() == component_id.lower():
            return vocabulary.describe(component)
    return vocabulary.get(component_id)


def components_of(obj) -> list:
    """Components the panel should draw: the load snapshot plus pending add/remove."""
    return edits.visible_components(store.component_json(obj), edits.read_structure(obj))


def merged_data(obj, component_id: str, data: dict) -> dict:
    """The payload as the panel shows it: document plus pending edits, copied because
    ``write_path`` mutates."""
    merged = copy.deepcopy(data) if isinstance(data, dict) else {}
    for path, value in edits.edited_fields(obj, component_id).items():
        edits.write_path(merged, path, copy.deepcopy(value))
    return merged


class PARADISE_ASSETS_OT_revert_field(Operator):
    """Forget a pending edit, so the field shows what the document says again."""

    bl_idname = "paradise_assets.revert_component_field"
    bl_label = "Revert Field"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    component_id: StringProperty(name="Component")
    #: Empty reverts the whole object, which is what the panel's header button asks for.
    field_name: StringProperty(name="Field")

    def execute(self, context):
        obj = context.active_object
        if not self.component_id:
            edits.clear(obj)
            self.report({"INFO"}, "Pending edits discarded")
        else:
            edits.clear(obj, self.component_id, self.field_name or None)
        return {"FINISHED"}


class PARADISE_ASSETS_OT_add_array_row(Operator):
    """Append one row to an authored list on the active object."""

    bl_idname = "paradise_assets.add_array_row"
    bl_label = "Add Row"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    component_id: StringProperty(name="Component")
    field_name: StringProperty(name="Field")

    def execute(self, context):
        obj = context.active_object
        schema = schema_for(context, obj, self.component_id)
        field = schema.resolve(self.field_name) if schema is not None else None
        if (
            field is None
            or field.type != "array"
            or field.items is None
            or component_schema.is_host_locked(field.items)
        ):
            self.report({"ERROR"}, f"{self.field_name} is not an authored list")
            return {"CANCELLED"}

        rows = _array_value(obj, self.component_id, self.field_name)
        sample = rows[0] if rows else None
        if component_schema.is_asset_field(field.items, sample):
            rows.append({})
        else:
            rows.append(copy.deepcopy(field.items.default_value()))
        edits.set_field(obj, self.component_id, self.field_name, rows)
        self.report({"INFO"}, f"Row added to {self.field_name} — save the document to write it")
        return {"FINISHED"}


class PARADISE_ASSETS_OT_remove_array_row(Operator):
    """Remove one row of an authored list on the active object."""

    bl_idname = "paradise_assets.remove_array_row"
    bl_label = "Remove Row"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    component_id: StringProperty(name="Component")
    field_name: StringProperty(name="Field")
    index: IntProperty(name="Index", min=0)

    def execute(self, context):
        obj = context.active_object
        rows = _array_value(obj, self.component_id, self.field_name)
        if self.index < 0 or self.index >= len(rows):
            self.report({"ERROR"}, f"{self.field_name} has no row {self.index}")
            return {"CANCELLED"}
        del rows[self.index]
        edits.set_field(obj, self.component_id, self.field_name, rows)
        return {"FINISHED"}


class PARADISE_ASSETS_OT_reveal_object(Operator):
    """Select a document object by identity and reveal it in the Outliner."""

    bl_idname = "paradise_assets.reveal_object"
    bl_label = "Show Object"
    bl_description = "Select this object and reveal it in the Outliner"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    guid: StringProperty(name="Guid")

    def execute(self, context):
        target = store.object_with_guid(context.scene, self.guid)
        if target is None:
            self.report({"ERROR"}, "No object in this scene has that identity")
            return {"CANCELLED"}
        for obj in context.selected_objects:
            obj.select_set(False)
        target.hide_set(False)
        target.hide_viewport = False
        target.select_set(True)
        context.view_layer.objects.active = target
        _reveal_in_outliner(context)
        return {"FINISHED"}


def _reveal_in_outliner(context) -> None:
    """Frame the active object in an Outliner, with an area override: from the 3D View sidebar
    ``outliner.show_active`` otherwise silently does nothing."""
    screen = getattr(context, "screen", None)
    if screen is None:
        return
    for area in screen.areas:
        if area.type != "OUTLINER":
            continue
        region = next((item for item in area.regions if item.type == "WINDOW"), None)
        if region is None:
            continue
        try:
            with context.temp_override(area=area, region=region):
                bpy.ops.outliner.show_active()
        except RuntimeError:
            pass
        return


def _array_value(obj, component_id: str, path: str) -> list:
    """The list at *path* as it currently shows, copied so a mutation does not alias storage."""
    data = {}
    for component in components_of(obj):
        if str(component.get("id", "")).lower() == component_id.lower():
            raw = component.get("data")
            data = raw if isinstance(raw, dict) else {}
            break
    value = edits.read_path(merged_data(obj, component_id, data), path)
    return copy.deepcopy(value) if isinstance(value, list) else []


_ADDABLE_CACHE: list[tuple[str, str, str]] = []


def _addable_items(self, context):
    """Vocabulary types not already on the object, for the panel's one Add button."""
    global _ADDABLE_CACHE
    obj = context.active_object if context is not None else None
    if obj is None:
        _ADDABLE_CACHE = [("NONE", "(nothing to add)", "")]
        return _ADDABLE_CACHE
    present = {str(component.get("id", "")).lower() for component in components_of(obj)}
    schemas = component_schema.addable(vocabulary_for(context), present)
    _ADDABLE_CACHE = [
        (schema.id, schema.display_name, schema.type or schema.id) for schema in schemas
    ] or [("NONE", "(nothing to add)", "")]
    return _ADDABLE_CACHE


class PARADISE_ASSETS_OT_add_component(Operator):
    """Attach one authored component to this object -- the panel's single Add button."""

    bl_idname = "paradise_assets.add_component"
    bl_label = "Add Component"
    bl_options = {"REGISTER", "UNDO"}
    bl_property = "component"

    component: EnumProperty(items=_addable_items)

    @classmethod
    def poll(cls, context) -> bool:
        obj = context.active_object
        return obj is not None and store.guid_of(obj) is not None

    def invoke(self, context, _event):
        context.window_manager.invoke_search_popup(self)
        return {"FINISHED"}

    def execute(self, context):
        if not self.component or self.component == "NONE":
            return {"CANCELLED"}
        obj = context.active_object
        vocabulary = vocabulary_for(context)
        schema = vocabulary.get(self.component) or component_schema.describe(
            {"id": self.component, "type": None, "data": {}}, vocabulary)
        if schema is None:
            self.report({"WARNING"}, f"'{self.component}' is not in the authoring schema.")
            return {"CANCELLED"}
        if component_schema.is_format_owned(schema.id) or component_schema.is_host_derived(schema.id):
            self.report({"ERROR"}, f"{schema.display_name} is authored by Blender, not this panel")
            return {"CANCELLED"}
        edits.add_component(obj, {
            "id": schema.id,
            "type": schema.type,
            "data": component_schema.default_payload(schema),
        })
        _redraw(context)
        self.report({"INFO"}, f"{schema.display_name} added — save the document to write it")
        return {"FINISHED"}


class PARADISE_ASSETS_OT_remove_component(Operator):
    """Remove this component from the object. meta and transform cannot go."""

    bl_idname = "paradise_assets.remove_component"
    bl_label = "Remove Component"
    bl_options = {"REGISTER", "UNDO"}

    component_id: StringProperty()

    def execute(self, context):
        if component_schema.is_format_owned(self.component_id):
            self.report({"ERROR"}, "meta and transform belong to Blender and cannot be removed")
            return {"CANCELLED"}
        obj = context.active_object
        if obj is None or not self.component_id:
            return {"CANCELLED"}
        edits.remove_component(obj, self.component_id)
        _redraw(context)
        return {"FINISHED"}


def _redraw(context) -> None:
    screen = getattr(context, "screen", None)
    if screen is None:
        return
    for area in screen.areas:
        area.tag_redraw()


classes = (
    PARADISE_ASSETS_OT_revert_field,
    PARADISE_ASSETS_OT_add_array_row,
    PARADISE_ASSETS_OT_remove_array_row,
    PARADISE_ASSETS_OT_reveal_object,
    PARADISE_ASSETS_OT_add_component,
    PARADISE_ASSETS_OT_remove_component,
)
