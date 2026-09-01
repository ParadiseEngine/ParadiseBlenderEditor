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

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, StringProperty
from bpy.types import Operator

from . import edits
from .document import component_schema, project
from .materialize import store

__all__ = ["classes", "vocabulary_for"]


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
    value_vector: FloatVectorProperty(name="Value", size=3)
    value_color: FloatVectorProperty(name="Value", size=4, subtype="COLOR", min=0.0, max=1.0)

    #: Which of the properties above this invocation is using.
    _slot = "value_string"
    _enum_items = None
    _field = None

    def invoke(self, context, event):
        obj = context.active_object
        schema = vocabulary_for(context).get(self.component_id)
        field = schema.field(self.field_name) if schema is not None else None
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
        label = field.name if field is not None else self.field_name
        if field is not None and field.unit:
            label = f"{label} ({field.unit})"
        layout.prop(self, self._slot, text=label)

    def execute(self, context):
        obj = context.active_object
        field = self._field
        value = getattr(self, self._slot)

        if self._slot in ("value_vector", "value_color"):
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


#: Which typed property each schema type is edited through.
_SLOTS = {
    "bool": "value_bool",
    "int": "value_int",
    "float": "value_float",
    "string": "value_string",
    "enum": "value_enum",
    "vector3": "value_vector",
    "color": "value_color",
}


def _current_value(obj, component_id: str, field_name: str, field):
    """What this field is right now: the pending edit, else the document, else the default."""
    pending = edits.edited_fields(obj, component_id)
    if field_name in pending:
        return pending[field_name]
    for component in store.component_json(obj):
        if str(component.get("id", "")).lower() == component_id.lower():
            data = component.get("data")
            if isinstance(data, dict) and field_name in data:
                return data[field_name]
            break
    return field.default


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
    if field.type == "vector3":
        return _numbers(value, 3, 0.0)
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


classes = (
    PARADISE_ASSETS_OT_edit_field,
    PARADISE_ASSETS_OT_revert_field,
)
