"""Bezier splines as document numbers (``HostSpline``, ParadiseEngine#362). No ``bpy`` here: what
a curve object means to the game is a contract, and it is tested without Blender.

A spline ROW is either the host spline itself (``authoredBy: spline`` on the row or the single
field -- a ``List<HostSpline>`` or one ``HostSpline``) or a game record with one member typed as
the host spline beside the game's own members (``Speed``, ``Loop`` ...). The curve decides the
geometry and nothing else, exactly as a shape Empty does for ``HostShape``.

**Knots and handles, never samples.** Per knot the position plus both handles as OFFSETS from it
(Blender stores handles as absolute positions; relative ones cannot be skewed by moving a knot in
another host), and the cyclic flag, in curve/spline/knot order. A copy, not a fit or a resample:
consumers choose their own sampling. Handle modes, tilt and radius are Blender's editing state and
stay in the ``.blend``.

**Object space**, document axes: a curve inside a prefab instance transforms with its instance,
the way a collider shape composes with its object. Blender's ``(x, y, z)`` is the document's
``(x, z, -y)`` (:mod:`.axes`); offsets are vectors, so they rotate the same way and never
translate.

The member names follow the kind as ParadiseEngine#362 proposes it; they are spelled once,
here, so the review that settles them changes one block.
"""

from __future__ import annotations

import math

__all__ = [
    "CYCLIC",
    "HANDLE_IN",
    "HANDLE_OUT",
    "HOST_KIND",
    "KNOTS",
    "MIN_KNOTS",
    "POSITION",
    "SplineError",
    "from_blender",
    "host_member",
    "is_spline_array",
    "is_spline_field",
    "is_spline_single",
    "keep_unchanged",
    "member_name",
    "spline_fields",
    "to_blender",
]

#: The ``authoredBy`` the schema dump puts on a ``HostSpline`` member.
HOST_KIND = "spline"

KNOTS = "Knots"
POSITION = "Position"
HANDLE_IN = "HandleIn"
HANDLE_OUT = "HandleOut"
CYCLIC = "Cyclic"

#: Fewer knots than this is not a curve, open or closed: a closed spline of two knots is a
#: valid loop (two arcs), and one knot is a point.
MIN_KNOTS = 2

_EPSILON = 1e-5


class SplineError(ValueError):
    """A curve the document cannot carry. The message names the object."""


# --- which schema fields are splines ----------------------------------------------------------


def _row_layout(field):
    """``(row type, member name or None)`` for a spline field, else ``None``."""
    row = getattr(field, "items", None) if field.type == "array" else field
    if row is None:
        return None
    if getattr(row, "authored_by", None) == HOST_KIND:
        return row, None
    member = host_member(field)
    return (row, member.name) if member is not None else None


def host_member(field):
    """The row member the curve decides -- the one typed as the host spline -- or ``None``."""
    row = getattr(field, "items", None) if field.type == "array" else field
    if row is None:
        return None
    for child in getattr(row, "fields", ()):
        if child.authored_by == HOST_KIND:
            return child
    return None


def is_spline_array(field) -> bool:
    """A LIST of spline rows: one row per spline of the curve, in spline order."""
    return field.type == "array" and _row_layout(field) is not None


def is_spline_single(field) -> bool:
    """ONE spline row: the curve must hold exactly one spline."""
    return field.type == "object" and _row_layout(field) is not None


def is_spline_field(field) -> bool:
    return is_spline_array(field) or is_spline_single(field)


def member_name(field) -> str | None:
    """Where a row keeps its geometry: a member's name, or ``None`` when the row IS the spline."""
    layout = _row_layout(field)
    return layout[1] if layout is not None else None


def spline_fields(schema):
    """Every spline field a component schema declares, list or single."""
    return [field for field in schema.fields if is_spline_field(field)]


# --- numbers -----------------------------------------------------------------------------------


def _to_document(v) -> list[float]:
    x, y, z = (float(c) for c in v)
    return [x, z, -y]


def _to_blender(v) -> tuple[float, float, float]:
    x, y, z = (float(c) for c in v)
    return (x, -z, y)


def _rounded(values) -> list[float]:
    # 6 places, as shapes do: float32 holds ~7 significant digits, and the canonical writer
    # would otherwise spell a value read back from Blender as 0.30000001192092896.
    return [round(v, 6) + 0.0 for v in values]


def from_blender(label: str, points, cyclic: bool) -> dict:
    """One Bezier spline as its document row geometry.

    ``points`` is ``[(co, handle_left, handle_right), ...]`` in Blender OBJECT space, absolute,
    as ``bpy.types.BezierSplinePoint`` stores them. ``label`` names the object in an error.
    """
    points = list(points)
    if len(points) < MIN_KNOTS:
        raise SplineError(
            f"{label}: a spline needs at least {MIN_KNOTS} knots to be a curve; this one has "
            f"{len(points)}. Add a knot or delete the spline.")
    knots = []
    for index, (co, left, right) in enumerate(points):
        co, left, right = (tuple(float(c) for c in v) for v in (co, left, right))
        if not all(math.isfinite(c) for v in (co, left, right) for c in v):
            raise SplineError(f"{label}: knot {index} has a coordinate that is not a finite number")
        knots.append({
            POSITION: _rounded(_to_document(co)),
            HANDLE_IN: _rounded(_to_document(tuple(a - b for a, b in zip(left, co, strict=True)))),
            HANDLE_OUT: _rounded(_to_document(tuple(a - b for a, b in zip(right, co, strict=True)))),
        })
    return {KNOTS: knots, CYCLIC: bool(cyclic)}


def to_blender(row: dict) -> tuple[list[tuple], bool]:
    """A document spline row as ``([(co, handle_left, handle_right), ...], cyclic)`` in Blender
    object space, absolute -- the inverse of :func:`from_blender`. Malformed knots are skipped:
    a document the game would refuse still loads, and the next save says what is wrong."""
    knots = row.get(KNOTS) if isinstance(row, dict) else None
    points = []
    for knot in knots if isinstance(knots, list) else ():
        if not isinstance(knot, dict):
            continue
        position = _vector(knot.get(POSITION))
        if position is None:
            continue
        handle_in = _vector(knot.get(HANDLE_IN)) or (0.0, 0.0, 0.0)
        handle_out = _vector(knot.get(HANDLE_OUT)) or (0.0, 0.0, 0.0)
        co = _to_blender(position)
        left = _to_blender(tuple(p + h for p, h in zip(position, handle_in, strict=True)))
        right = _to_blender(tuple(p + h for p, h in zip(position, handle_out, strict=True)))
        points.append((co, left, right))
    return points, bool(row.get(CYCLIC)) if isinstance(row, dict) else False


def keep_unchanged(stored: dict, computed: dict) -> dict:
    """``computed``, except that a knot the curve did not move keeps the document's own
    spelling: Blender stores float32, and re-deriving every number on every save would rewrite
    a file nobody touched (the rule the object transform and collider shapes follow too)."""
    merged = dict(computed)
    old_knots = stored.get(KNOTS) if isinstance(stored, dict) else None
    new_knots = computed.get(KNOTS) or []
    if isinstance(old_knots, list):
        kept = []
        for index, knot in enumerate(new_knots):
            old = old_knots[index] if index < len(old_knots) else None
            if not isinstance(old, dict):
                kept.append(knot)
                continue
            same = {
                key: old[key] if _same_numbers(old.get(key), value) else value
                for key, value in knot.items()
            }
            # A knot written with extra members (a future kind's) keeps them.
            for key, value in old.items():
                same.setdefault(key, value)
            kept.append(same)
        merged[KNOTS] = kept
    if isinstance(stored, dict) and stored.get(CYCLIC) == computed.get(CYCLIC):
        merged[CYCLIC] = stored[CYCLIC]
    return merged


def _same_numbers(a, b) -> bool:
    if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)) or len(a) != len(b):
        return False
    try:
        return all(
            abs(float(x) - float(y)) <= _EPSILON * max(abs(float(x)), 1.0)
            for x, y in zip(a, b, strict=True)
        )
    except (TypeError, ValueError):
        return False


def _vector(value) -> tuple[float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        out = tuple(float(v) for v in value)
    except (TypeError, ValueError):
        return None
    return out if all(math.isfinite(v) for v in out) else None  # type: ignore[return-value]
