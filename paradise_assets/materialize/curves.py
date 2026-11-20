"""Bezier curves as document objects: a CURVE object is the editing surface for a ``HostSpline``
field, and the save bakes its splines into that field (#76, ParadiseEngine#362).

The host half of a composed kind, shaped like :mod:`.shapes`: the schema marks the field
``authoredBy: spline``, Blender's own curve editing draws it, and the save copies the curve's
Bezier knots and handles into the rows -- the row's other members (a route's speed, a rail's
name) stay the panel's. The numbers live in :mod:`..document.splines`.

- **Admission is a gesture, never a side effect.** ``Add Curve to Document`` gives a CURVE object
  an identity and the component that carries its splines; a curve nobody admitted stays
  Blender's own content, exactly as a stray Empty does.
- **The document carries the geometry, the ``.blend`` the editing state.** The load rebuilds the
  curve from the baked knots (handle types ``ALIGNED`` where the two handles are collinear,
  ``FREE`` otherwise); tilt, radius and resolution are Blender's.
- **Never export geometry.** A curve is document data like an Empty: no GLB carries it.
"""

from __future__ import annotations

import copy
import uuid

import bpy
from mathutils import Vector

from .. import edits
from ..document import component_schema, splines
from . import shapes, store

__all__ = [
    "admit",
    "bake",
    "carries_splines",
    "curve_components",
    "new_object",
    "spline_field_of",
]


def spline_field_of(schema):
    """The ONE spline field a component declares, or ``None``. A component with two would make a
    curve's splines ambiguous; the first wins on load and the save refuses the second."""
    found = splines.spline_fields(schema) if schema is not None else []
    return found[0] if found else None


def curve_components(vocabulary) -> list:
    """Every component the game declares a spline field on, in display-name order."""
    return [schema for schema in vocabulary if spline_field_of(schema) is not None]


def carries_splines(components: list, vocabulary) -> bool:
    """Whether a payload list holds a component with a spline field -- the object is then a
    curve, rows or not, so the author has something to draw in."""
    return _first_spline(components, vocabulary) is not None


def _first_spline(components: list, vocabulary):
    for component in components:
        schema = vocabulary.describe(component) if vocabulary is not None else None
        field = spline_field_of(schema)
        if field is not None:
            data = component.get("data") if isinstance(component.get("data"), dict) else {}
            return field, data
    return None


def new_object(name: str, components: list, vocabulary):
    """A CURVE object built from the first spline field ``components`` carry, or ``None`` when
    none does. Object-space knots; the caller places the object."""
    found = _first_spline(components, vocabulary)
    if found is None:
        return None
    field, data = found
    curve = bpy.data.curves.new(name, "CURVE")
    curve.dimensions = "3D"
    member = splines.member_name(field)
    for row in _stored_rows(field, data.get(field.name)):
        geometry = row if member is None else row.get(member)
        if not isinstance(geometry, dict):
            continue
        points, cyclic = splines.to_blender(geometry)
        if not points:
            continue
        spline = curve.splines.new("BEZIER")
        spline.bezier_points.add(len(points) - 1)
        for point, (co, left, right) in zip(spline.bezier_points, points, strict=True):
            point.co = co
            point.handle_left_type = point.handle_right_type = "FREE"
            point.handle_left = left
            point.handle_right = right
            if _collinear(Vector(co), Vector(left), Vector(right)):
                point.handle_left_type = point.handle_right_type = "ALIGNED"
        spline.use_cyclic_u = cyclic
    return bpy.data.objects.new(name, curve)


def bake(obj, entry, vocabulary, item_default) -> int:
    """Write ``obj``'s Bezier splines into the spline field of ``entry``'s components. Returns
    how many rows changed. Only what the object's OWN entry authors is baked, as for shapes.

    Raises :class:`~..document.splines.SplineError` (a ``ValueError``, which the save turns
    into a refusal) for anything the document cannot carry, naming the object.
    """
    if obj.type != "CURVE":
        return 0
    baked_field = None
    changed = 0
    for component in entry.components:
        if component.removed:
            continue
        payload = {"id": component.id, "type": component.type, "data": component.data}
        schema = vocabulary.describe(payload)
        if schema is None:
            continue
        for field in splines.spline_fields(schema):
            if baked_field is not None:
                raise splines.SplineError(
                    f"{obj.name}: both {baked_field} and {schema.display_name}.{field.name} take "
                    "this curve's splines. Keep one, or draw the second on its own curve object.")
            baked_field = f"{schema.display_name}.{field.name}"
            changed += _bake_field(obj, component.data, field, item_default)
    return changed


def _bake_field(obj, data: dict, field, item_default) -> int:
    live = _live_splines(obj)
    if splines.is_spline_single(field) and len(live) > 1:
        raise splines.SplineError(
            f"{obj.name}: {field.name} holds ONE spline and the curve has {len(live)}. "
            "Join them or separate the extra ones into their own object.")
    member = splines.member_name(field)
    stored_rows = _stored_rows(field, data.get(field.name))
    rows, changed = [], 0
    for index, geometry in enumerate(live):
        original = stored_rows[index] if index < len(stored_rows) else None
        base = copy.deepcopy(original) if isinstance(original, dict) else item_default(field)
        if member is None:
            held = base
        else:
            held = base.get(member)
            held = held if isinstance(held, dict) else {}
        merged = splines.keep_unchanged(held, geometry)
        changed += int(any(held.get(key) != value for key, value in merged.items()))
        held.update(merged)
        if member is not None:
            base[member] = held
        rows.append(base)
    changed += abs(len(rows) - len(stored_rows))
    _store_rows(field, data, rows)
    return changed


def _live_splines(obj) -> list[dict]:
    """Each spline of the curve as its document geometry, in spline order."""
    found = []
    for index, spline in enumerate(obj.data.splines):
        label = f"{obj.name} (spline {index})"
        if spline.type != "BEZIER":
            raise splines.SplineError(
                f"{label} is a {spline.type.title()} spline; only Bezier splines are baked. "
                "Set its type to Bezier (Spline > Set Spline Type) or delete it.")
        points = [
            (tuple(p.co), tuple(p.handle_left), tuple(p.handle_right))
            for p in spline.bezier_points
        ]
        found.append(splines.from_blender(label, points, spline.use_cyclic_u))
    return found


def admit(context, component_id: str):
    """Make the active CURVE object a document object carrying ``component_id``. Returns the
    object. Raises ``ValueError`` with an author-facing message when it cannot."""
    obj = context.active_object
    if obj is None or obj.type != "CURVE":
        raise ValueError("Select a curve object first.")
    if store.guid_of(obj) is not None:
        raise ValueError(f"{obj.name} is already a document object; add the component instead.")
    scene = context.scene
    roots = [o for o in scene.collection.all_objects
             if store.guid_of(o) is not None and not store.is_derived(o) and o.parent is None]
    if len(roots) != 1:
        raise ValueError("This document has no single root to hang the curve under.")
    if obj.parent is not None and store.guid_of(obj.parent) is None:
        raise ValueError(
            f"{obj.name} hangs under {obj.parent.name}, which is not a document object. "
            "Unparent it or parent it to a document object first.")
    from .. import component_ops
    vocabulary = component_ops.vocabulary_for(context)
    schema = vocabulary.get(component_id)
    if schema is None or spline_field_of(schema) is None:
        raise ValueError(f"'{component_id}' declares no spline field in the game's schema.")

    store.tag_object(obj, str(uuid.uuid4()), [])
    store.tag_name(obj, obj.name)
    if obj.parent is None:
        context.view_layer.update()
        world = obj.matrix_world.copy()
        obj.parent = roots[0]
        obj.matrix_parent_inverse.identity()
        obj.matrix_world = world
    edits.add_component(obj, {
        "id": schema.id,
        "type": schema.type,
        "data": component_schema.default_payload(schema),
    })
    return obj


def _stored_rows(field, value) -> list:
    return shapes._stored_rows(field, value)


def _store_rows(field, data: dict, rows: list) -> None:
    shapes._store_rows(field, data, rows)


def _collinear(co: Vector, left: Vector, right: Vector) -> bool:
    a, b = left - co, right - co
    if a.length < 1e-9 or b.length < 1e-9:
        return False
    return a.normalized().dot(b.normalized()) < -1.0 + 1e-6
