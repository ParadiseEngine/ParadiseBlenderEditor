"""Editing one component field, and reverting one.

**One operator per VERB, not per type.** Blender registers an operator's properties at class
definition time, so a schema-driven editor cannot declare "the property this field needs" -- the
field is not known until the panel draws. The way around it is a fixed set of typed properties,
one per shape the schema can describe, of which exactly one is used per invocation. That is why
:class:`PARADISE_ASSETS_OT_edit_field` looks like it has six values and does not: it has one, six
times over, and ``_slot`` is what says which.

The dialog is deliberate rather than inline. Blender can draw an ID property straight into a
panel, which would be fewer clicks -- but ID properties normalize types on the way in and out (an
``int`` returns a ``float``, a tuple a list), and these values are written into a document whose
numbers are a cross-language contract. A dialog lets the value stay in a property of the RIGHT
type and be written through :mod:`.edits` exactly once, in the shape the schema declares.
"""

from __future__ import annotations

import copy

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import Operator

from . import edits
from .document import component_schema, project
from .materialize import store

__all__ = ["classes", "components_of", "merged_data", "schema_for", "vocabulary_for"]


def vocabulary_for(context) -> component_schema.Vocabulary:
    """The game's component vocabulary for the open document's project.

    Resolved on demand rather than cached on the scene: the dump is a build product, and an
    author who rebuilds the game to add a component expects the panel to show it without
    reopening the document. It is a small file read at draw time -- if that ever costs, the fix
    is a cache keyed on its mtime, not a copy taken at load.
    """
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
    """The component payload as the panel should show it: the document, plus pending edits.

    A copy, because :func:`edits.write_path` mutates. The ID property holding the original
    payload is display data and must not be written back from.
    """
    merged = copy.deepcopy(data) if isinstance(data, dict) else {}
    for path, value in edits.edited_fields(obj, component_id).items():
        edits.write_path(merged, path, copy.deepcopy(value))
    return merged


def _enum_values(self, context):
    """The enum's allowed values, as Blender items.

    A callback rather than a static list, because the values belong to the field being edited.
    The reference is kept on the operator (``_enum_items``) because Blender does not retain the
    strings a callback returns -- returning freshly built tuples each call is the documented way
    to get garbage-collected labels.
    """
    return getattr(self, "_enum_items", None) or [("NONE", "—", "")]


class PARADISE_ASSETS_OT_edit_field(Operator):
    """Change one field of one component on the active object."""

    bl_idname = "paradise_assets.edit_component_field"
    bl_label = "Edit Field"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    component_id: StringProperty(name="Component")
    field_name: StringProperty(name="Field")

    value_bool: BoolProperty(name="Value")
    value_int: IntProperty(name="Value")
    value_float: FloatProperty(name="Value")
    value_string: StringProperty(name="Value")
    value_enum: EnumProperty(name="Value", items=_enum_values)
    value_vector2: FloatVectorProperty(name="Value", size=2)
    value_vector: FloatVectorProperty(name="Value", size=3)
    value_quaternion: FloatVectorProperty(name="Value", size=4, subtype="QUATERNION")
    value_color: FloatVectorProperty(name="Value", size=4, subtype="COLOR", min=0.0, max=1.0)

    #: Which of the properties above this invocation is using.
    _slot = "value_string"
    _enum_items = None
    _field = None

    def invoke(self, context, event):
        obj = context.active_object
        schema = schema_for(context, obj, self.component_id)
        field = schema.resolve(self.field_name) if schema is not None else None
        if field is None or not field.editable:
            self.report({"ERROR"}, f"{self.field_name} is not an editable field")
            return {"CANCELLED"}

        self._field = field
        self._slot = _SLOTS.get(field.type, "value_string")
        current = _current_value(obj, self.component_id, self.field_name, field)

        if field.type == "enum":
            # Blender rejects an empty enum identifier, so a field whose schema lists no values
            # cannot be offered as one. Falling back to a string keeps it editable rather than
            # unreachable, which matters because the value still has to reach the document.
            items = [(value, value, "") for value in field.values if value]
            if not items:
                self._slot = "value_string"
                self.value_string = str(current or "")
            else:
                type(self)._enum_items = items
                self.value_enum = current if current in field.values else items[0][0]
        else:
            setattr(self, self._slot, _coerce(current, field))

        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        layout = self.layout
        field = self._field
        if field is not None and field.doc:
            column = layout.column(align=True)
            for line in _wrap(field.doc, 46):
                column.label(text=line)
        label = self.field_name if field is None else (
            field.name if "/" not in self.field_name else self.field_name
        )
        if field is not None and field.unit:
            label = component_schema.field_caption(label, field.unit)
        layout.prop(self, self._slot, text=label)

    def execute(self, context):
        obj = context.active_object
        field = self._field
        value = getattr(self, self._slot)

        if self._slot in ("value_vector", "value_vector2", "value_quaternion", "value_color"):
            value = [float(component) for component in value]
        elif self._slot == "value_float":
            value = float(value)
        elif self._slot == "value_int":
            value = int(value)
        elif self._slot == "value_bool":
            value = bool(value)
        else:
            value = str(value)

        if field is not None:
            value = field.clamp(value)

        edits.set_field(obj, self.component_id, self.field_name, value)
        self.report({"INFO"}, f"{self.field_name} edited — save the document to write it")
        return {"FINISHED"}


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
    """Frame the active object in an Outliner editor, when the layout has one.

    The operator lives in the 3D View sidebar, so without an override `outliner.show_active`
    would look at the wrong area and silently do nothing -- which is the whole point of the
    button. A layout with no Outliner still selected the object; that is enough to find it.
    """
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


#: Which typed property each schema type is edited through.
_SLOTS = {
    "bool": "value_bool",
    "int": "value_int",
    "float": "value_float",
    "string": "value_string",
    "enum": "value_enum",
    "vector2": "value_vector2",
    "vector3": "value_vector",
    "quaternion": "value_quaternion",
    "color": "value_color",
}


def _current_value(obj, component_id: str, field_name: str, field):
    """What this field is right now: the pending overlay over the document, else the default."""
    data = {}
    for component in components_of(obj):
        if str(component.get("id", "")).lower() == component_id.lower():
            raw = component.get("data")
            data = raw if isinstance(raw, dict) else {}
            break
    value = edits.read_path(merged_data(obj, component_id, data), field_name)
    return field.default_value() if value is None else value


def _coerce(value, field):
    """*value* as the type its widget needs, tolerating whatever the document holds.

    A document is allowed to carry a number where the schema says float and an int is what was
    written, or a colour as ``#RRGGBBAA`` rather than four components. Refusing to open the
    dialog over that would leave the field uneditable for exactly the values most in need of a
    fix, so each conversion falls back to the field's default and then to a neutral one.
    """
    if field.type == "bool":
        return bool(value) if isinstance(value, (bool, int, float)) else False
    if field.type in ("int", "float"):
        try:
            return int(value) if field.type == "int" else float(value)
        except (TypeError, ValueError):
            return 0
    if field.type == "vector2":
        return _numbers(value, 2, 0.0)
    if field.type == "vector3":
        return _numbers(value, 3, 0.0)
    if field.type == "quaternion":
        numbers = _numbers(value, 4, 0.0)
        return [0.0, 0.0, 0.0, 1.0] if numbers == [0.0, 0.0, 0.0, 0.0] else numbers
    if field.type == "color":
        return _color(value)
    return str(value) if value is not None else ""


def _numbers(value, count: int, fill: float) -> list[float]:
    if isinstance(value, (list, tuple)):
        out = [float(v) if isinstance(v, (int, float)) else fill for v in value[:count]]
        return out + [fill] * (count - len(out))
    return [fill] * count


def _color(value) -> list[float]:
    """A colour as RGBA floats, from either spelling the contract uses.

    Since engine 0.32.0 a ``Color32`` writes as ``"#RRGGBBAA"`` -- a packed value rendered as
    four floats was an exact value wearing a lossy costume -- but documents written before that,
    and hand-written ones, carry ``{r, g, b, a}``. Both are read; the write side always produces
    the object form, which every reader still accepts.
    """
    if isinstance(value, str) and value.startswith("#") and len(value) in (7, 9):
        try:
            raw = [int(value[i:i + 2], 16) / 255.0 for i in range(1, len(value) - 1, 2)]
        except ValueError:
            return [1.0, 1.0, 1.0, 1.0]
        return raw + [1.0] * (4 - len(raw))
    if isinstance(value, dict):
        return [float(value.get(key, 1.0)) for key in ("r", "g", "b", "a")]
    return _numbers(value, 4, 1.0) if isinstance(value, (list, tuple)) else [1.0, 1.0, 1.0, 1.0]


def _wrap(text: str, width: int) -> list[str]:
    """A doc string as panel-width lines. Blender labels do not wrap."""
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
    return lines[:4]


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
    PARADISE_ASSETS_OT_edit_field,
    PARADISE_ASSETS_OT_revert_field,
    PARADISE_ASSETS_OT_add_array_row,
    PARADISE_ASSETS_OT_remove_array_row,
    PARADISE_ASSETS_OT_reveal_object,
    PARADISE_ASSETS_OT_add_component,
    PARADISE_ASSETS_OT_remove_component,
)
