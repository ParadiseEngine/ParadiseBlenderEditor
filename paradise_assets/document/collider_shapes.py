"""The geometry of a host-authored collision shape, as numbers. No ``bpy`` here: what a shape
Empty's transform means is a contract with the game (``Paradise.Authoring.HostShape``), and it
is tested without Blender.

One Empty per shape, parented to the object it collides for. Its LOCAL transform is the shape's
placement, and the primitive's extents ride on the scale so the ordinary gizmos edit them:

- ``Box``: a CUBE display of half-size 0.5, so the Empty's scale IS ``Size``.
- ``Sphere``: a SPHERE display whose size is ``Radius``; a non-uniform scale folds to the largest
  axis, so the sphere stays enclosing.
- ``Capsule``: a CUBE display of half-size 0.5 scaled ``(2r, h, 2r)`` in document axes -- the
  capsule is Y-aligned in the document, so rotate the Empty to orient it. Blender has no capsule
  display; the box is its envelope.
"""

from __future__ import annotations

from . import axes

__all__ = [
    "GEOMETRY_FIELDS", "HOST_KIND", "SHAPE_TYPES",
    "from_gizmo", "is_shape_array", "keep_unchanged", "to_gizmo",
]

#: The ``authoredBy`` the schema dump puts on a ``HostShape`` member.
HOST_KIND = "shape"

SHAPE_TYPES = ("Box", "Sphere", "Capsule")

#: What the Empty decides; every other member of a shape row stays the panel's to edit.
GEOMETRY_FIELDS = frozenset(
    {"ShapeType", "LocalCenter", "LocalRotation", "Size", "Radius", "Height"})

_IDENTITY_ROTATION = (0.0, 0.0, 0.0, 1.0)
_EPSILON = 1e-5


def is_shape_array(field) -> bool:
    """Whether a schema field is a list of host shapes."""
    return (
        field.type == "array"
        and field.items is not None
        and field.items.authored_by == HOST_KIND
    )


def to_gizmo(shape: dict):
    """A shape row as ``(display, display_size, position, rotation, scale)`` in Blender axes."""
    shape_type = str(shape.get("ShapeType") or "Box")
    center = _vector(shape.get("LocalCenter"), (0.0, 0.0, 0.0))
    rotation = _vector(shape.get("LocalRotation"), _IDENTITY_ROTATION, 4)

    if shape_type == "Sphere":
        radius = float(shape.get("Radius") or 0.0)
        position, rot, scale = axes.to_blender_trs(center, rotation, (1.0, 1.0, 1.0))
        return "SPHERE", radius, position, rot, scale
    if shape_type == "Capsule":
        radius = float(shape.get("Radius") or 0.0)
        height = float(shape.get("Height") or 0.0)
        extents = (2.0 * radius, height, 2.0 * radius)
        position, rot, scale = axes.to_blender_trs(center, rotation, extents)
        return "CUBE", 0.5, position, rot, scale
    size = _vector(shape.get("Size"), (1.0, 1.0, 1.0))
    position, rot, scale = axes.to_blender_trs(center, rotation, size)
    return "CUBE", 0.5, position, rot, scale


def from_gizmo(shape_type: str, display_size: float, position, rotation, scale) -> dict:
    """The geometry members a Blender TRS spells, in document axes."""
    center, rot, extents = axes.from_blender_trs(
        tuple(float(v) for v in position),
        tuple(float(v) for v in rotation),
        tuple(float(v) for v in scale),
    )
    sx, sy, sz = (abs(v) for v in extents)
    geometry = {
        "ShapeType": shape_type,
        "LocalCenter": [round(v, 6) for v in center],
        "LocalRotation": [round(v, 6) for v in rot],
        "Size": [0.0, 0.0, 0.0],
        "Radius": 0.0,
        "Height": 0.0,
    }
    if shape_type == "Sphere":
        geometry["Radius"] = round(float(display_size) * max(sx, sy, sz), 6)
    elif shape_type == "Capsule":
        geometry["Radius"] = round(max(sx, sz) / 2.0, 6)
        geometry["Height"] = round(sy, 6)
    else:
        geometry["Size"] = [round(sx, 6), round(sy, 6), round(sz, 6)]
    return geometry


def keep_unchanged(stored: dict, computed: dict) -> dict:
    """``computed``, except that a member the Empty did not move keeps the document's own
    spelling: a rebase round-trips to ~1e-7 and would otherwise rewrite every shape on every
    save (the same rule the object transform follows)."""
    merged = dict(computed)
    for key, value in computed.items():
        authored = stored.get(key)
        if authored is None:
            continue
        if key == "LocalRotation":
            same = _same_rotation(authored, value)
        elif isinstance(value, list):
            same = _same_numbers(authored, value)
        elif isinstance(value, (int, float)) and isinstance(authored, (int, float)):
            same = abs(float(authored) - float(value)) <= _EPSILON * max(abs(float(authored)), 1.0)
        else:
            same = authored == value
        if same:
            merged[key] = authored
    return merged


def _same_numbers(a, b) -> bool:
    if not isinstance(a, (list, tuple)) or len(a) != len(b):
        return False
    return all(abs(float(x) - float(y)) <= _EPSILON * max(abs(float(x)), 1.0) for x, y in zip(a, b))


def _same_rotation(a, b) -> bool:
    if not isinstance(a, (list, tuple)) or len(a) != 4:
        return False
    dot = abs(sum(float(x) * float(y) for x, y in zip(a, b)))
    return abs(1.0 - dot) <= _EPSILON


def _vector(value, default, count: int = 3):
    if isinstance(value, (list, tuple)) and len(value) == count:
        try:
            return tuple(float(v) for v in value)
        except (TypeError, ValueError):
            return default
    return default
