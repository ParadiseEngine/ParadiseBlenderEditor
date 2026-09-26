"""Inline typed widgets for the Components panel: one typed RNA property per visible field,
on the WindowManager (the one ID type ``draw()`` may write), rebuilt from document plus overlay.
Only the overlay is ever saved. ``[AuthorRange]`` is applied in the update callback, not as
ID-property min/max: writing an ID during a draw is what made a rigidbody edit wipe the
component list.
"""

from __future__ import annotations

import json
import math
import os
import re

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
from .materialize import light_preview, store

__all__ = ["attach", "classes", "detach", "draw_item", "sync"]

_NONE = "NONE"
_SLOTS_KEY = "paradise_field_slots"
_FINGERPRINT_KEY = "paradise_field_fingerprint"
_OVERLAY_KEY = "paradise_field_overlay"
_SYNCING = False
_INT_MIN, _INT_MAX = -(2**31), 2**31 - 1
_ENUM_CACHE: dict[str, list] = {}
_PICK_CACHE: list[tuple[str, str, str]] = []

#: Widgets that already print a unit, so the label must not repeat it.
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


def _layout_of(context, scope="component"):
    if scope == "document":
        return store.project_of(context.scene) if context is not None else None
    state = store.read_state(context.scene) if context is not None else None
    if state is None:
        return None
    return project.locate(state.path)


def _pick_asset_items(self, context):
    """Search-popup items, held module-level because Blender does not retain callback tuples."""
    global _PICK_CACHE
    kinds = json.loads(self.kinds_json or "[]")
    layout = _layout_of(context, self.scope)
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
    obj = _owner(context, slot.scope)
    if obj is None or not slot.component_id or not slot.path:
        return
    try:
        value = _clamp_slot(slot, _overlay_value(slot, context))
    except ValueError as error:
        slot.validation_error = str(error)
        _SYNCING = True
        try:
            slot.value_integer_text = slot.accepted_integer_text
        finally:
            _SYNCING = False
        return
    slot.validation_error = ""
    _SYNCING = True
    try:
        _assign_rna(slot, value)
    finally:
        _SYNCING = False
    edits.set_field(obj, slot.component_id, slot.path, value)
    if slot.scope != "document":
        light_preview.refresh(context.scene)


def _owner(context, scope):
    if context is None:
        return None
    if scope == "document":
        return context.window_manager
    from .component_ops import document_object
    return document_object(context)


class ParadiseFieldSlot(PropertyGroup):
    """One visible field: the path it addresses, and the typed property currently showing it."""

    component_id: StringProperty()
    scope: StringProperty(default="component")
    path: StringProperty()
    rna: StringProperty()
    kinds_json: StringProperty()
    range_min: FloatProperty()
    range_max: FloatProperty()
    has_min: BoolProperty()
    has_max: BoolProperty()
    integer_range_json: StringProperty(default="[null, null]")
    accepted_integer_text: StringProperty()
    validation_error: StringProperty()

    value_bool: BoolProperty(update=_commit)
    value_int: IntProperty(update=_commit)
    value_integer_text: StringProperty(update=_commit)
    value_float: FloatProperty(update=_commit)
    # One RNA property per unit so Blender draws the unit on the field.
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
    if rna == "value_color":
        return dict(zip("rgba", (float(component) for component in slot.value_color), strict=True))
    if rna in ("value_vector", "value_vector2", "value_quaternion"):
        return [float(component) for component in getattr(slot, rna)]
    if rna in ("value_float", "value_factor", "value_mass", "value_distance",
               "value_angle", "value_time"):
        return float(getattr(slot, rna))
    if rna == "value_int":
        return int(slot.value_int)
    if rna == "value_integer_text":
        text = slot.value_integer_text.strip()
        if re.fullmatch(r"[+-]?[0-9]+", text) is None:
            raise ValueError("Enter a decimal whole number")
        value = int(text)
        minimum, maximum = json.loads(slot.integer_range_json)
        if minimum is not None and value < minimum:
            raise ValueError(f"Value must be at least {minimum}")
        if maximum is not None and value > maximum:
            raise ValueError(f"Value must be at most {maximum}")
        return value
    if rna == "value_bool":
        return bool(slot.value_bool)
    if rna == "value_enum":
        return str(slot.value_enum)
    return str(slot.value_string)


def _clamp_slot(slot, value):
    """Hold a number to the field's ``[AuthorRange]``, when the slot recorded one."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    if slot.rna in ("value_int", "value_integer_text"):
        minimum, maximum = json.loads(slot.integer_range_json)
        if minimum is not None:
            value = max(value, minimum)
        if maximum is not None:
            value = min(value, maximum)
        return int(value)
    if slot.has_min:
        value = max(value, slot.range_min)
    if slot.has_max:
        value = min(value, slot.range_max)
    return float(value)


def _assign_rna(slot, value) -> None:
    rna = slot.rna
    if rna in ("value_float", "value_factor", "value_mass", "value_distance",
               "value_angle", "value_time"):
        setattr(slot, rna, float(value))
    elif rna == "value_int":
        slot.value_int = int(value)
    elif rna == "value_integer_text":
        slot.value_integer_text = str(value)
        slot.accepted_integer_text = slot.value_integer_text


def draw_item(
    layout, context, obj, component_id: str, item, value, edited: dict, row=None,
    overridden=frozenset(), scope="component",
) -> None:
    """One plan row as a widget. Asset references are a search popup, not an EnumProperty:
    rewriting that enum on every redraw is what made a picked slot stick."""
    row = row or layout.row(align=True)
    slot = _slot_for(context, component_id, item.path, scope)
    unit = None if (slot is not None and slot.rna in _RNA_SHOWS_UNIT) else item.field.unit
    label = component_schema.field_caption(
        item.path if "/" in item.path else item.field.name,
        unit,
    )
    if component_schema.is_asset_field(item.field, value):
        _draw_asset(row, component_id, item, value, label, scope)
        _draw_revert(row, edited, component_id, item.path, scope)
        if scope != "document":
            _draw_prefab_revert(row, component_id, item.path, overridden)
        return
    if slot is None:
        row.label(text=f"{label}: {component_schema.format_value(value)}")
        _draw_prefab_revert(row, component_id, item.path, overridden)
        return
    row.prop(
        slot, slot.rna, text=label,
        slider=slot.rna == "value_factor",
    )
    if slot.validation_error:
        warning = layout.row()
        warning.alert = True
        warning.label(text=slot.validation_error, icon="ERROR")
    _draw_revert(row, edited, component_id, item.path, scope)
    if scope != "document":
        _draw_prefab_revert(row, component_id, item.path, overridden)


def _draw_asset(row, component_id: str, item, value, label: str, scope="component") -> None:
    guid, path = _asset_parts(value)
    caption = path or "(none)"
    indexed = item.path.rsplit("/", 1)[-1].isdigit()
    if not indexed:
        row.label(text=f"{label}:")
    pick = row.operator("paradise_assets.pick_asset", text=caption, icon="ASSET_MANAGER")
    pick.component_id = component_id
    pick.scope = scope
    pick.field_name = item.path
    pick.kinds_json = json.dumps(_kinds_of(item.field, value))
    pick.current_guid = guid
    pick.current_path = path


def _draw_revert(row, edited: dict, component_id: str, path: str, scope="component") -> None:
    if not _path_is_edited(edited, path):
        return
    if any(path.startswith(key + "/") for key in edited):
        return
    if scope == "document":
        revert = row.operator("paradise_assets.edit_project_structure", text="", icon="LOOP_BACK")
        revert.action = "revert"
    else:
        revert = row.operator("paradise_assets.revert_component_field", text="", icon="LOOP_BACK")
        revert.component_id = component_id
    revert.field_name = path


def _draw_prefab_revert(row, component_id: str, path: str, overridden) -> None:
    """The badge on a field whose value is this instance's rather than its prefab's, and the
    button that hands it back. ``DECORATE_OVERRIDE`` is the icon Blender itself uses for a
    library override, so it reads without a legend.

    Only the TOP-LEVEL field is marked: `resolve._merge_data` is shallow, so an override
    replaces a whole sub-table and the field the author overrode is the outermost one.
    """
    if path.split("/")[0] not in overridden:
        return
    revert = row.operator(
        "paradise_assets.revert_to_prefab", text="", icon="DECORATE_OVERRIDE")
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


def sync(context, obj, rows: list[tuple[str, object, object]], scope="component") -> None:
    """Keep WM slots matched to *obj*'s plan. Rebuilt only when the path set changes (a rebuild
    mid-drag steals the slider); values refresh only when the overlay changes (an enum rewritten
    every frame refused a second choice). Reloads and schema changes invalidate the snapshot
    fingerprint even when the set of field paths stays unchanged."""
    wm = context.window_manager
    slots_key, fingerprint_key, overlay_key = _keys(scope)
    fingerprint = _fingerprint(obj, rows, scope)
    overlay_fp = json.dumps(edits.read(obj), sort_keys=True)
    slots = getattr(wm, slots_key)
    if wm.get(fingerprint_key) != fingerprint:
        slots.clear()
        for component_id, item, value in rows:
            slot = slots.add()
            slot.component_id = component_id
            slot.scope = scope
            slot.path = item.path
            slot.rna = _rna_of(item.field, value)
            slot.kinds_json = json.dumps(_kinds_of(item.field, value))
            _bind_range(slot, item.field)
            _write_slot(slot, item.field, value, context)
        wm[fingerprint_key] = fingerprint
        wm[overlay_key] = overlay_fp
        return
    if wm.get(overlay_key) == overlay_fp:
        return
    by_path = {(slot.component_id, slot.path): slot for slot in slots}
    for component_id, item, value in rows:
        slot = by_path.get((component_id, item.path))
        if slot is not None:
            _write_slot(slot, item.field, value, context)
    wm[overlay_key] = overlay_fp


def _keys(scope):
    return ((_SLOTS_KEY, _FINGERPRINT_KEY, _OVERLAY_KEY) if scope != "document" else
            ("paradise_document_slots", "paradise_document_fingerprint", "paradise_document_overlay"))


def _fingerprint(obj, rows, scope="component") -> str:
    if scope == "document":
        from .project_settings import DATA_KEY, ID_KEY, ROOT_KEY
        identity = [obj.get(ROOT_KEY), obj.get(ID_KEY), obj.get(DATA_KEY)]
    else:
        identity = [store.guid_of(obj) or obj.name, store.component_json(obj)]
    schema = [(component_id, item.path, item.field.type, item.field.default, item.field.unit,
               item.field.minimum, item.field.maximum, item.field.values, item.field.asset_kinds,
               _rna_of(item.field, value))
              for component_id, item, value in rows]
    return json.dumps([identity, schema], sort_keys=True)


def _slot_for(context, component_id: str, path: str, scope="component"):
    for slot in getattr(context.window_manager, _keys(scope)[0], []):
        if slot.component_id == component_id and slot.path == path:
            return slot
    return None


def _rna_of(field, value) -> str:
    if field.type == "float":
        return _UNIT_RNA.get(field.unit or "", "value_float")
    if field.type == "int":
        for number in (field.minimum, field.maximum, value, field.default):
            if isinstance(number, (int, float)) and (number < _INT_MIN or number > _INT_MAX):
                return "value_integer_text"
        return "value_int"
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
    if field.type == "int":
        # RNA floats round uint.MaxValue up by one. Keep integer limits outside float RNA.
        slot.integer_range_json = json.dumps([
            math.ceil(field.minimum) if slot.has_min else None,
            math.floor(field.maximum) if slot.has_max else None,
        ])
        return
    slot.range_min = float(field.minimum) if slot.has_min else 0.0
    slot.range_max = float(field.maximum) if slot.has_max else 0.0
    if field.unit == "unit01":
        slot.has_min = True
        slot.has_max = True
        slot.range_min = 0.0
        slot.range_max = 1.0


#: Extensions that name the same KIND of thing, so a picker offered one offers the others. Only
#: models have such a set; every other document kind is one suffix.
_INTERCHANGEABLE = ((".glb", ".gltf", ".blend", ".fbx"),)


def _kinds_of(field, value) -> list:
    """Which extensions the picker offers. The schema's ``assetKinds`` when the game declares
    them, else the extension the value already carries -- and an EMPTY list, meaning every
    identified asset, when neither says.

    Guessing a suffix is worse than offering everything: a slot holding
    ``materials/x.material`` under a schema with no ``assetKinds`` used to fall through to a
    hardcoded ``.toml``, which listed every config in the project and not one material."""
    if field.values:
        return list(field.values)
    if field.asset_kinds:
        return list(field.asset_kinds)

    path = value.get("path") if isinstance(value, dict) else value
    if not isinstance(path, str):
        return []

    suffix = os.path.splitext(path)[1].lower()
    if not suffix:
        return []
    for pair in _INTERCHANGEABLE:
        if suffix in pair:
            return list(pair)
    return [suffix]


def _write_slot(slot, field, value, context) -> None:
    global _SYNCING
    _SYNCING = True
    try:
        if value is None and not field.optional:
            value = field.default_value()
        rna = slot.rna
        slot.validation_error = ""
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
        elif rna == "value_integer_text":
            slot.value_integer_text = "" if value is None else str(value)
            slot.accepted_integer_text = slot.value_integer_text
            try:
                _overlay_value(slot, context)
            except ValueError as error:
                slot.validation_error = str(error)
        elif rna in ("value_float", "value_factor", "value_mass", "value_distance",
                     "value_angle", "value_time"):
            try:
                setattr(slot, rna, float(_clamp_slot(slot, float(value))))
            except (TypeError, ValueError):
                setattr(slot, rna, 0.0)
        elif rna in ("value_vector2", "value_vector", "value_quaternion", "value_color"):
            count = {"value_vector2": 2, "value_vector": 3}.get(rna, 4)
            fill = 1.0 if rna in ("value_color",) else 0.0
            if rna == "value_color" and isinstance(value, dict):
                numbers = [value.get(channel, 1.0) for channel in "rgba"]
            else:
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
    scope: StringProperty(default="component")
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
        obj = _owner(context, self.scope)
        if obj is None or not self.component_id or not self.field_name:
            return {"CANCELLED"}
        if not self.asset or self.asset == _NONE:
            edits.set_field(obj, self.component_id, self.field_name, {})
        else:
            path = self.current_path
            guid = self.asset
            for ident, label, sidecar_guid in _PICK_CACHE:
                if ident == self.asset:
                    path = "" if label.endswith(" (missing)") else label
                    # The sidecar's spelling, not the enum identifier: the identifier is
                    # lowercased for matching only.
                    guid = sidecar_guid or guid
                    break
            edits.set_field(
                obj, self.component_id, self.field_name,
                {"guid": guid, "path": path},
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
    bpy.types.WindowManager.paradise_document_slots = CollectionProperty(type=ParadiseFieldSlot)


def detach() -> None:
    if hasattr(bpy.types.WindowManager, "paradise_field_slots"):
        del bpy.types.WindowManager.paradise_field_slots
    if hasattr(bpy.types.WindowManager, "paradise_field_fingerprint"):
        del bpy.types.WindowManager.paradise_field_fingerprint
    if hasattr(bpy.types.WindowManager, "paradise_document_slots"):
        del bpy.types.WindowManager.paradise_document_slots


classes = (ParadiseFieldSlot, PARADISE_ASSETS_OT_pick_asset)
