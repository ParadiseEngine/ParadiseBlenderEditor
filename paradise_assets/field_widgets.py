"""Inline typed widgets for the Components panel.

The dialog was a workaround: Blender ID properties coerce types, so a value had to live on a
properly typed RNA property for the length of an edit. A PropertyGroup slot is the same
workaround without the popup -- one typed property per visible field, drawn with ``layout.prop``.

Slots live on the WindowManager, not the object. They are display state rebuilt from the
document plus overlay; the overlay is still the only thing save reads.

Numbers with a schema unit use a dedicated RNA property so Blender can draw the unit on the
field itself (``kg``, ``m``, a 0–1 factor). ``[AuthorRange]`` is applied in the update callback
-- a drag past the cap snaps back -- rather than as ID-property min/max. Those have to live on
an ID, and writing an ID during a panel draw is what made a rigidbody edit wipe the component
list.
"""

from __future__ import annotations

import json

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import Operator, PropertyGroup

from . import edits
from .document import assets as asset_index
from .document import component_schema, project
from .materialize import store

__all__ = ["attach", "classes", "detach", "draw_item", "sync"]

_NONE = "NONE"
_SLOTS_KEY = "paradise_field_slots"
_FINGERPRINT_KEY = "paradise_field_fingerprint"
_OVERLAY_KEY = "paradise_field_overlay"
_SYNCING = False
_ENUM_CACHE: dict[str, list] = {}
_PICK_CACHE: list[tuple[str, str, str]] = []

#: RNA properties whose widget already prints a unit (or is a 0–1 factor). The label must not
#: also say ``(kg)`` or the row reads ``Mass (kg): 1.0 kg``.
_RNA_SHOWS_UNIT = frozenset({
    "value_factor", "value_mass", "value_distance", "value_angle", "value_time",
})

_UNIT_RNA = {
    "unit01": "value_factor",
    "kilograms": "value_mass",
    "meters": "value_distance",
    "radians": "value_angle",
    "seconds": "value_time",
}


def _enum_items(self, context):
    """Allowed values for this slot's enum, cached because Blender does not retain callback tuples."""
    raw = self.kinds_json or "[]"
    cached = _ENUM_CACHE.get(raw)
    if cached is None:
        values = json.loads(raw) if raw else []
        cached = [(value, value, "") for value in values if value] or [(_NONE, "—", "")]
        _ENUM_CACHE[raw] = cached
    return cached


def _layout_of(context):
    state = store.read_state(context.scene) if context is not None else None
    if state is None:
        return None
    return project.locate(state.path)


def _pick_asset_items(self, context):
    """Search-popup items for one asset field: GUID identifiers, path labels.

    Held in a module-level list because Blender does not retain tuples a callback returns.
    """
    global _PICK_CACHE
    kinds = json.loads(self.kinds_json or "[]")
    layout = _layout_of(context)
    items = [(_NONE, "(none)", "Keep empty")]
    if layout is not None:
        for asset in asset_index.list_assets(layout, kinds or None):
            items.append((asset.guid.lower(), asset.path, asset.guid))
    current = (self.current_guid or "").lower()
    if current and current != _NONE and all(item[0] != current for item in items):
        label = self.current_path or current
        items.append((current, f"{label} (missing)", ""))
    _PICK_CACHE = items
    return _PICK_CACHE


def _commit(slot, context) -> None:
    """RNA changed: write the overlay in the shape the document stores."""
    global _SYNCING
    if _SYNCING:
        return
    obj = context.active_object if context is not None else None
    if obj is None or not slot.component_id or not slot.path:
        return
    value = _clamp_slot(slot, _overlay_value(slot, context))
    _SYNCING = True
    try:
        _assign_rna(slot, value)
    finally:
        _SYNCING = False
    edits.set_field(obj, slot.component_id, slot.path, value)


class ParadiseFieldSlot(PropertyGroup):
    """One visible field: the path it addresses, and the typed property currently showing it."""

    component_id: StringProperty()
    path: StringProperty()
    rna: StringProperty()
    kinds_json: StringProperty()
    range_min: FloatProperty()
    range_max: FloatProperty()
    has_min: BoolProperty()
    has_max: BoolProperty()

    value_bool: BoolProperty(update=_commit)
    value_int: IntProperty(update=_commit)
    value_float: FloatProperty(update=_commit)
    # Closed unit vocabulary: one RNA property each, so Blender can put the unit on the field
    # and so ``[Unit01]`` is a 0–1 slider rather than a free drag.
    value_factor: FloatProperty(
        min=0.0, max=1.0, soft_min=0.0, soft_max=1.0, subtype="FACTOR", update=_commit)
    value_mass: FloatProperty(min=0.0, unit="MASS", update=_commit)
    value_distance: FloatProperty(subtype="DISTANCE", unit="LENGTH", update=_commit)
    value_angle: FloatProperty(subtype="ANGLE", unit="ROTATION", update=_commit)
    value_time: FloatProperty(subtype="TIME", unit="TIME", update=_commit)
    value_string: StringProperty(update=_commit)
    value_enum: EnumProperty(items=_enum_items, update=_commit)
    value_vector2: FloatVectorProperty(size=2, update=_commit)
    value_vector: FloatVectorProperty(size=3, update=_commit)
    value_quaternion: FloatVectorProperty(size=4, update=_commit)
    value_color: FloatVectorProperty(size=4, subtype="COLOR", min=0.0, max=1.0, update=_commit)


def _overlay_value(slot, context):
    rna = slot.rna
    if rna in ("value_vector", "value_vector2", "value_quaternion", "value_color"):
        return [float(component) for component in getattr(slot, rna)]
    if rna in ("value_float", "value_factor", "value_mass", "value_distance",
               "value_angle", "value_time"):
        return float(getattr(slot, rna))
    if rna == "value_int":
        return int(slot.value_int)
    if rna == "value_bool":
        return bool(slot.value_bool)
    if rna == "value_enum":
        return str(slot.value_enum)
    return str(slot.value_string)


def _clamp_slot(slot, value):
    """Hold a number to the field's ``[AuthorRange]``, when the slot recorded one."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    if slot.has_min:
        value = max(value, slot.range_min)
    if slot.has_max:
        value = min(value, slot.range_max)
    return int(value) if slot.rna == "value_int" else float(value)


def _assign_rna(slot, value) -> None:
    rna = slot.rna
    if rna in ("value_float", "value_factor", "value_mass", "value_distance",
               "value_angle", "value_time"):
        setattr(slot, rna, float(value))
    elif rna == "value_int":
        slot.value_int = int(value)


def draw_item(layout, context, obj, component_id: str, item, value, edited: dict, row=None) -> None:
    """One plan row as an embedded widget, plus revert when the overlay has touched it.

    Asset references are a search popup, not an EnumProperty: a dropdown of every ``.toml`` is
    not searchable, and rewriting that enum on every redraw is what made a picked slot stick.
    """
    row = row or layout.row(align=True)
    slot = _slot_for(context, component_id, item.path)
    unit = None if (slot is not None and slot.rna in _RNA_SHOWS_UNIT) else item.field.unit
    label = component_schema.field_caption(
        item.path if "/" in item.path else item.field.name,
        unit,
    )
    if component_schema.is_asset_field(item.field, value):
        _draw_asset(row, component_id, item, value, label)
        _draw_revert(row, edited, component_id, item.path)
        return
    if slot is None:
        row.label(text=f"{label}: {component_schema.format_value(value)}")
        return
    row.prop(
        slot, slot.rna, text=label,
        slider=slot.rna == "value_factor",
    )
    _draw_revert(row, edited, component_id, item.path)


def _draw_asset(row, component_id: str, item, value, label: str) -> None:
    guid, path = _asset_parts(value)
    caption = path or "(none)"
    indexed = item.path.rsplit("/", 1)[-1].isdigit()
    if not indexed:
        row.label(text=f"{label}:")
    pick = row.operator("paradise_assets.pick_asset", text=caption, icon="ASSET_MANAGER")
    pick.component_id = component_id
    pick.field_name = item.path
    pick.kinds_json = json.dumps(_kinds_of(item.field, value))
    pick.current_guid = guid
    pick.current_path = path


def _draw_revert(row, edited: dict, component_id: str, path: str) -> None:
    if not _path_is_edited(edited, path):
        return
    revert = row.operator("paradise_assets.revert_component_field", text="", icon="LOOP_BACK")
    revert.component_id = component_id
    revert.field_name = path


def _asset_parts(value) -> tuple[str, str]:
    if isinstance(value, dict):
        guid = value.get("guid") if isinstance(value.get("guid"), str) else ""
        path = value.get("path") if isinstance(value.get("path"), str) else ""
        return guid, path
    if isinstance(value, str):
        return "", value
    return "", ""


def sync(context, obj, rows: list[tuple[str, object, object]]) -> None:
    """Keep WM slots matched to the visible plan of *obj*.

    *rows* is ``(component_id, plan_item, value)`` for every leaf/row that should have a widget.
    The collection is rebuilt only when the set of paths changes -- rewriting it on every overlay
    tick would steal the slider out from under a drag. Values refresh when the overlay itself
    changes (revert, add/remove), not every redraw: rewriting an enum every frame is what made
    a picked dropdown refuse a second choice.
    """
    wm = context.window_manager
    fingerprint = _fingerprint(obj, rows)
    overlay_fp = json.dumps(edits.read(obj), sort_keys=True)
    slots = getattr(wm, _SLOTS_KEY)
    if wm.get(_FINGERPRINT_KEY) != fingerprint:
        slots.clear()
        for component_id, item, value in rows:
            slot = slots.add()
            slot.component_id = component_id
            slot.path = item.path
            slot.rna = _rna_of(item.field, value)
            slot.kinds_json = json.dumps(_kinds_of(item.field, value))
            _bind_range(slot, item.field)
            _write_slot(slot, item.field, value, context)
        wm[_FINGERPRINT_KEY] = fingerprint
        wm[_OVERLAY_KEY] = overlay_fp
        return
    if wm.get(_OVERLAY_KEY) == overlay_fp:
        return
    by_path = {(slot.component_id, slot.path): slot for slot in slots}
    for component_id, item, value in rows:
        slot = by_path.get((component_id, item.path))
        if slot is not None:
            _write_slot(slot, item.field, value, context)
    wm[_OVERLAY_KEY] = overlay_fp


def _fingerprint(obj, rows) -> str:
    guid = store.guid_of(obj) or obj.name
    paths = ",".join(f"{component_id}:{item.path}" for component_id, item, _ in rows)
    return f"{guid}|{paths}"


def _slot_for(context, component_id: str, path: str):
    for slot in getattr(context.window_manager, _SLOTS_KEY, []):
        if slot.component_id == component_id and slot.path == path:
            return slot
    return None


def _rna_of(field, value) -> str:
    if field.type == "float":
        return _UNIT_RNA.get(field.unit or "", "value_float")
    return {
        "bool": "value_bool",
        "int": "value_int",
        "enum": "value_enum",
        "vector2": "value_vector2",
        "vector3": "value_vector",
        "quaternion": "value_quaternion",
        "color": "value_color",
        "string": "value_string",
    }.get(field.type, "value_string")


def _bind_range(slot, field) -> None:
    slot.has_min = isinstance(field.minimum, (int, float))
    slot.has_max = isinstance(field.maximum, (int, float))
    slot.range_min = float(field.minimum) if slot.has_min else 0.0
    slot.range_max = float(field.maximum) if slot.has_max else 0.0
    if field.unit == "unit01":
        slot.has_min = True
        slot.has_max = True
        slot.range_min = 0.0
        slot.range_max = 1.0


def _kinds_of(field, value) -> list:
    if field.values:
        return list(field.values)
    if field.asset_kinds:
        return list(field.asset_kinds)
    path = ""
    if isinstance(value, dict):
        path = value.get("path") or ""
    elif isinstance(value, str):
        path = value
    if isinstance(path, str) and path.endswith(".toml"):
        return [".toml"]
    if isinstance(path, str) and (path.endswith(".glb") or path.endswith(".gltf")):
        return [".glb", ".gltf"]
    if component_schema.is_asset_field(field, value):
        return [".toml"]
    return []


def _write_slot(slot, field, value, context) -> None:
    global _SYNCING
    _SYNCING = True
    try:
        rna = slot.rna
        if rna == "value_enum":
            text = str(value or "")
            allowed = [item[0] for item in _enum_items(slot, context)]
            slot.value_enum = text if text in allowed else allowed[0]
        elif rna == "value_bool":
            slot.value_bool = bool(value) if isinstance(value, (bool, int, float)) else False
        elif rna == "value_int":
            try:
                slot.value_int = int(_clamp_slot(slot, int(value)))
            except (TypeError, ValueError):
                slot.value_int = 0
        elif rna in ("value_float", "value_factor", "value_mass", "value_distance",
                     "value_angle", "value_time"):
            try:
                setattr(slot, rna, float(_clamp_slot(slot, float(value))))
            except (TypeError, ValueError):
                setattr(slot, rna, 0.0)
        elif rna in ("value_vector2", "value_vector", "value_quaternion", "value_color"):
            count = {"value_vector2": 2, "value_vector": 3}.get(rna, 4)
            fill = 1.0 if rna in ("value_color",) else 0.0
            numbers = list(value) if isinstance(value, (list, tuple)) else []
            padded = [float(n) if isinstance(n, (int, float)) else fill for n in numbers[:count]]
            padded += [0.0 if rna == "value_quaternion" else fill] * (count - len(padded))
            if rna == "value_quaternion":
                while len(padded) < 4:
                    padded.append(0.0)
                if padded == [0.0, 0.0, 0.0, 0.0]:
                    padded = [0.0, 0.0, 0.0, 1.0]
            setattr(slot, rna, padded)
        else:
            slot.value_string = "" if value is None else str(value)
    except (TypeError, ValueError):
        pass
    finally:
        _SYNCING = False


class PARADISE_ASSETS_OT_pick_asset(Operator):
    """Choose a project file for one asset-reference field, searchable by path."""

    bl_idname = "paradise_assets.pick_asset"
    bl_label = "Pick Asset"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}
    bl_property = "asset"

    component_id: StringProperty()
    field_name: StringProperty()
    kinds_json: StringProperty()
    current_guid: StringProperty()
    current_path: StringProperty()
    asset: EnumProperty(items=_pick_asset_items)

    def invoke(self, context, _event):
        current = (self.current_guid or "").lower()
        self.asset = current if current else _NONE
        context.window_manager.invoke_search_popup(self)
        return {"FINISHED"}

    def execute(self, context):
        obj = context.active_object
        if obj is None or not self.component_id or not self.field_name:
            return {"CANCELLED"}
        if not self.asset or self.asset == _NONE:
            edits.set_field(obj, self.component_id, self.field_name, {})
        else:
            path = self.current_path
            for ident, label, _ in _PICK_CACHE:
                if ident == self.asset:
                    path = "" if label.endswith(" (missing)") else label
                    break
            edits.set_field(
                obj, self.component_id, self.field_name,
                {"guid": self.asset, "path": path},
            )
        _redraw(context)
        return {"FINISHED"}


def _redraw(context) -> None:
    screen = getattr(context, "screen", None)
    if screen is None:
        return
    for area in screen.areas:
        area.tag_redraw()


def _path_is_edited(edited: dict, path: str) -> bool:
    return any(
        key == path or key.startswith(path + "/") or path.startswith(key + "/")
        for key in edited
    )


def attach() -> None:
    bpy.types.WindowManager.paradise_field_slots = CollectionProperty(type=ParadiseFieldSlot)
    bpy.types.WindowManager.paradise_field_fingerprint = StringProperty()


def detach() -> None:
    if hasattr(bpy.types.WindowManager, "paradise_field_slots"):
        del bpy.types.WindowManager.paradise_field_slots
    if hasattr(bpy.types.WindowManager, "paradise_field_fingerprint"):
        del bpy.types.WindowManager.paradise_field_fingerprint


classes = (ParadiseFieldSlot, PARADISE_ASSETS_OT_pick_asset)
