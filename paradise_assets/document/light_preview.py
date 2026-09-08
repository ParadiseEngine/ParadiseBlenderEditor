"""A light preview from declared schema semantics, never from component names or Blender data."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .component_schema import Vocabulary


@dataclass(frozen=True)
class LightPreview:
    kind: str
    color: tuple[float, float, float]
    energy: float
    radius: float
    range: float
    outer: float
    blend: float
    shadows: bool
    direction: tuple[float, float, float]


def describe(components: list[dict], vocabulary: Vocabulary) -> LightPreview | None:
    declared = [(component, vocabulary.get(component.get("id"))) for component in components]
    kinds = [schema.preview_light for _, schema in declared if schema and schema.preview_light]
    if not kinds:
        return None
    if len(kinds) != 1 or kinds[0] not in {"Directional", "Point", "Spot"}:
        raise ValueError("a preview requires exactly one declared light archetype")
    values = {}
    for component, schema in declared:
        if schema is not None:
            _collect(schema.fields, component.get("data") or {}, values)
    kind = kinds[0]
    outer = min(179.0, max(0.1, _number(values, "OuterDegrees", 45.0)))
    inner = min(outer, max(0.0, _number(values, "InnerDegrees", 0.0)))
    colour = values.get("Color", [1.0, 1.0, 1.0])
    if isinstance(colour, dict):
        colour = [colour.get(c, 1.0) for c in "rgb"]
    colour = _vector(colour, (1.0, 1.0, 1.0))
    return LightPreview(
        kind,
        tuple(_linear(max(0.0, min(1.0, c))) for c in colour),
        max(0.0, _number(values, "Intensity", 1.0)) * (1.0 if kind == "Directional" else 100.0),
        max(0.0, _number(values, "Size", 0.0)),
        max(0.0, _number(values, "Range", 0.0)),
        math.radians(outer),
        1.0 - inner / outer,
        bool(values.get("Shadows", False)),
        _vector(values.get("Direction"), (0.0, -1.0, 0.0)),
    )


def _collect(fields, data, values):
    for field in fields:
        value = (
            data.get(field.name, field.default_value()) if isinstance(data, dict) else field.default_value()
        )
        if field.light_field:
            if field.light_field in values:
                raise ValueError(f"two fields author light parameter {field.light_field}")
            values[field.light_field] = value
        if field.fields:
            _collect(field.fields, value, values)
        # A light has one value per parameter. Array rows cannot select which value owns it.
        if field.items and _has_light_field(field.items):
            raise ValueError("light preview parameters cannot be declared inside an array")


def _has_light_field(field):
    return (
        bool(field.light_field)
        or any(_has_light_field(child) for child in field.fields)
        or (field.items is not None and _has_light_field(field.items))
    )


def _number(values, field, fallback):
    value = values.get(field, fallback)
    return float(value) if isinstance(value, (float, int)) and math.isfinite(value) else fallback


def _vector(value, fallback):
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return fallback
    if not all(isinstance(v, (float, int)) and math.isfinite(v) for v in value[:3]):
        return fallback
    return tuple(float(v) for v in value[:3])


def _linear(value):
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
