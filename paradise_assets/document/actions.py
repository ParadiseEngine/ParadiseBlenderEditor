"""The host-independent authored action transport contract."""

from __future__ import annotations

import math
from dataclasses import dataclass


class ActionSchema:
    def __init__(self, raw: dict):
        self.name = str(raw["name"])
        self.display_name = raw.get("displayName") or self.name
        self.doc = raw.get("doc") or ""
        self.kind = raw.get("kind", "button")
        # onSave is the pre-"save"-kind spelling emitted by engine packages before the unified
        # action schema; either form marks the action for post-save dispatch.
        self.on_save = raw.get("onSave") is True


@dataclass(frozen=True)
class Overlay:
    id: str
    visible: bool
    vertices: tuple
    triangles: tuple
    color: tuple


@dataclass(frozen=True)
class Response:
    toggles: dict
    document_changed: bool
    overlays: tuple[Overlay, ...]


def _numbers(value, count=None):
    return isinstance(value, list) and (count is None or len(value) == count) and all(
        type(item) in (int, float) and math.isfinite(item) for item in value)


def response(payload) -> Response:
    """Validate a complete response before applying any viewport or toggle state."""
    if not isinstance(payload, dict):
        raise ValueError("Invalid authored action response")
    toggles = payload.get("toggles", {})
    if not isinstance(toggles, dict) or any(
            not isinstance(key, str) or type(value) is not bool for key, value in toggles.items()):
        raise ValueError("Invalid authored action toggle state")
    changed = payload.get("documentChanged", False)
    if type(changed) is not bool or not isinstance(payload.get("overlays", []), list):
        raise ValueError("Invalid authored action response")
    overlays = []
    for item in payload.get("overlays", []):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError("An action overlay needs an id")
        visible = item.get("visible", True)
        if type(visible) is not bool:
            raise ValueError("Invalid action overlay visibility")
        if not visible:
            overlays.append(Overlay(item["id"], False, (), (), ()))
            continue
        vertices, indices = item.get("vertices"), item.get("indices")
        color = item.get("color", [0.1, 0.8, 0.6, 0.35])
        if not _numbers(vertices) or len(vertices) % 3 or not _numbers(color, 4):
            raise ValueError("Invalid action overlay vertices or color")
        if not isinstance(indices, list) or len(indices) % 3 or any(
                type(index) is not int or not 0 <= index < len(vertices) // 3 for index in indices):
            raise ValueError("Invalid action overlay triangle indices")
        # The action speaks engine Y-up; a Blender overlay is Z-up, without altering geometry.
        points = tuple((vertices[i], -vertices[i + 2], vertices[i + 1])
                       for i in range(0, len(vertices), 3))
        triangles = tuple(tuple(indices[i:i + 3]) for i in range(0, len(indices), 3))
        overlays.append(Overlay(item["id"], True, points, triangles, tuple(color)))
    return Response(dict(toggles), changed, tuple(overlays))


def arguments(document, component, action, entity, state_path, response_path, *, value=None, on_save=False):
    result = ["assets", "invoke-action", str(document), component, action,
              "--entity", entity, "--state", str(state_path), "--response", str(response_path)]
    if value is not None:
        result += ["--value", "true" if value else "false"]
    if on_save:
        result.append("--on-save")
    return result
