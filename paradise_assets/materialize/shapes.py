"""Collision shapes as Blender Empties an author moves, rotates and scales.

The schema marks a collider's shape list ``authoredBy: shape``: the HOST draws and bakes it, the
panel does not type it. This is the host half. Each shape row becomes a child Empty of the object
tagged with which component and row it stands for; on save the Empty's local transform is baked
back into that row's geometry (:mod:`..document.collider_shapes`), while the row's other members
-- layer, trigger, name -- stay the panel's. Deleting the Empty deletes the shape.

Shape Empties carry no document identity: they are not objects, and nothing that walks document
objects sees them. The one place that must know is ``load._clear_previous``, or a reload keeps
the last document's shapes beside this one's.
"""

from __future__ import annotations

import copy
import json

import bpy
from mathutils import Quaternion, Vector

from ..document import collider_shapes, component_schema

__all__ = [
    "SHAPE_KEY", "add_shape", "bake", "default_row", "is_shape", "materialize", "overlay_live", "owner_of",
    "remove_all", "shape_empties",
]

#: JSON ``{"component": id, "field": path, "index": row, "shape": ShapeType}``.
SHAPE_KEY = "paradise_shape"


def is_shape(obj) -> bool:
    return obj is not None and SHAPE_KEY in obj


def owner_of(obj):
    """The document object a shape Empty collides for."""
    return obj.parent if is_shape(obj) and obj.parent is not None else None


def materialize(obj, components: list, vocabulary: component_schema.Vocabulary) -> int:
    """Create one Empty per shape row of every host-shape list ``obj``'s components carry."""
    made = 0
    for component in components:
        schema = vocabulary.describe(component)
        if schema is None:
            continue
        data = component.get("data") if isinstance(component.get("data"), dict) else {}
        component_id = str(component.get("id", ""))
        for field in collider_shapes.shape_fields(schema):
            member = collider_shapes.member_name(field)
            for index, row in enumerate(_stored_rows(field, data.get(field.name))):
                if isinstance(row, dict):
                    geometry = row if member is None else row.get(member)
                    _create(obj, component_id, field.name, index,
                            geometry if isinstance(geometry, dict) else {})
                    made += 1
    return made


def add_shape(obj, component_id: str, field_name: str, shape_type: str, *, single: bool = False):
    """A new shape Empty at the object's origin, numbered after the last one. A SINGLE field
    (a marker's Volume) holds one: a second is refused rather than silently dropped on save."""
    existing = shape_empties(obj, component_id, field_name)
    if single and existing:
        raise ValueError(f"{field_name} is one shape, and it already has one")
    index = max((_tag(e)["index"] for e in existing), default=-1) + 1
    defaults = {"Box": {"Size": [1.0, 1.0, 1.0]},
                "Sphere": {"Radius": 0.5},
                "Capsule": {"Radius": 0.25, "Height": 1.0}}[shape_type]
    return _create(obj, component_id, field_name, index, {"ShapeType": shape_type, **defaults})


def shape_empties(obj, component_id: str, field_name: str) -> list:
    """This list's Empties, in row order."""
    found = []
    for child in obj.children:
        tag = _tag(child)
        if tag is None:
            continue
        if tag["component"].lower() == component_id.lower() and tag["field"] == field_name:
            found.append(child)
    found.sort(key=lambda e: _tag(e)["index"])
    return found


def bake(obj, entry, vocabulary: component_schema.Vocabulary, item_default) -> int:
    """Write the shape Empties into ``entry``'s shape lists. Returns how many rows the Empties
    changed. A row whose Empty is gone is dropped; a new Empty gains a default row. Afterwards
    the Empties are renumbered to the rows they now are."""
    changed = 0
    for component in entry.components:
        payload = {"id": component.id, "type": component.type, "data": component.data}
        schema = vocabulary.describe(payload)
        if schema is None:
            continue
        for field in collider_shapes.shape_fields(schema):
            stored_rows = _stored_rows(field, component.data.get(field.name))
            live = _live_rows(obj, component.id, field, stored_rows, item_default)
            rows = []
            for position, (empty, tag, row, moved) in enumerate(live):
                changed += moved
                rows.append(row)
                tag["index"] = position
                empty[SHAPE_KEY] = json.dumps(tag)
            if len(rows) != len(stored_rows):
                changed += abs(len(rows) - len(stored_rows))
            _store_rows(field, component.data, rows)
    return changed


def overlay_live(obj, component_id: str, schema, data: dict, item_default) -> dict:
    """``data`` with every shape list replaced by what the Empties say NOW -- the rows the save
    would write. The panel plans from this, or it keeps showing the rows of the last save while
    an author adds and deletes Empties."""
    for field in collider_shapes.shape_fields(schema):
        stored_rows = _stored_rows(field, data.get(field.name))
        _store_rows(field, data, [
            row for _, _, row, _ in _live_rows(obj, component_id, field, stored_rows, item_default)
        ])
    return data


def _stored_rows(field, value) -> list:
    """The document's rows for a shape field: the list itself, or a single row as a list of one.
    A single field nobody filled is absent, and reads as no rows."""
    if field.type == "array":
        return value if isinstance(value, list) else []
    return [value] if isinstance(value, dict) else []


def _store_rows(field, data: dict, rows: list) -> None:
    """The inverse: a list is written whole; a single field takes its one row, or is REMOVED
    when there is none -- the game reads an absent Volume as null and refuses the marker, which
    is the right answer for a trigger nobody drew."""
    if field.type == "array":
        if rows or field.name in data:
            data[field.name] = rows
        return
    if rows:
        data[field.name] = rows[0]
    else:
        data.pop(field.name, None)


def _live_rows(obj, component_id: str, field, stored_rows: list, item_default) -> list:
    """Per shape Empty, in row order: ``(empty, tag, row, changed)`` where ``row`` is the stored
    row it stands for (or a default one) with the Empty's geometry baked into the host member."""
    member = collider_shapes.member_name(field)
    out = []
    for empty in shape_empties(obj, component_id, field.name):
        tag = _tag(empty)
        original = stored_rows[tag["index"]] if tag["index"] < len(stored_rows) else None
        base = copy.deepcopy(original) if isinstance(original, dict) else item_default(field)
        if member is None:
            held = base
        else:
            held = base.get(member)
            held = held if isinstance(held, dict) else {}
        computed = collider_shapes.from_gizmo(
            tag["shape"], empty.empty_display_size,
            empty.location, _quaternion_xyzw(empty), empty.scale)
        geometry = collider_shapes.keep_unchanged(held, computed)
        changed = int(any(held.get(k) != v for k, v in geometry.items()))
        held.update(geometry)
        if member is not None:
            base[member] = held
        out.append((empty, tag, base, changed))
    return out


def default_row(field) -> dict:
    """A new shape row: every member at its schema default, so the game reads a complete record
    rather than one whose omitted keys it has to guess."""
    row = field.items if field.type == "array" else field
    if getattr(row, "authored_by", None) == collider_shapes.HOST_KIND:
        return {}   # the row is the geometry, and the Empty fills every member of it
    return {child.name: child.default_value() for child in row.fields}


def remove_all(scene) -> None:
    for obj in [o for o in scene.collection.all_objects if is_shape(o)]:
        bpy.data.objects.remove(obj, do_unlink=True)


def _create(owner, component_id: str, field_name: str, index: int, row: dict):
    display, size, position, rotation, scale = collider_shapes.to_gizmo(row)
    shape_type = str(row.get("ShapeType") or "Box")
    empty = bpy.data.objects.new(f"{owner.name}.{shape_type}", None)
    empty.empty_display_type = display
    empty.empty_display_size = size
    empty.show_in_front = True
    empty[SHAPE_KEY] = json.dumps(
        {"component": component_id, "field": field_name, "index": index, "shape": shape_type})
    for collection in owner.users_collection:
        collection.objects.link(empty)
    empty.parent = owner
    empty.matrix_parent_inverse.identity()
    empty.location = Vector(position)
    empty.rotation_mode = "QUATERNION"
    x, y, z, w = rotation
    empty.rotation_quaternion = Quaternion((w, x, y, z))
    empty.scale = Vector(scale)
    return empty


def _quaternion_xyzw(empty):
    if empty.rotation_mode == "QUATERNION":
        q = empty.rotation_quaternion
    elif empty.rotation_mode == "AXIS_ANGLE":
        angle, x, y, z = empty.rotation_axis_angle
        q = Quaternion((x, y, z), angle)
    else:
        q = empty.rotation_euler.to_quaternion()
    if q.w < 0.0:
        q.negate()
    return (q.x, q.y, q.z, q.w)


def _tag(obj) -> dict | None:
    raw = obj.get(SHAPE_KEY) if obj is not None else None
    if not isinstance(raw, str):
        return None
    try:
        tag = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(tag, dict) or not isinstance(tag.get("component"), str):
        return None
    tag.setdefault("field", "Shapes")
    tag["index"] = int(tag.get("index", 0))
    tag.setdefault("shape", "Box")
    return tag

