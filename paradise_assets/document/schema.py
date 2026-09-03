"""Which component fields name a MESH, read from the dump's ``authoredBy: mesh``. The dump is
an enrichment, not a requirement: a fresh clone has none, so the fallback is "a string ending
in .glb". A wrong guess costs a wrong preview, not data, since payload writes never consult
this; that is what makes a heuristic acceptable here and nowhere else.
"""

from __future__ import annotations

import json
import os

__all__ = ["MeshFields", "load"]

#: Where a dumped schema is looked for, relative to the project root, in order.
_CANDIDATES = (".editor/authoring-schema.json", "build/authoring-schema.json", "data/authoring-schema.json")


class MeshFields:
    """Which ``(component type, field name)`` pairs hold a mesh reference."""

    def __init__(self, pairs: set[tuple[str, str]] | None, source: str | None) -> None:
        self._pairs = pairs
        #: Where the schema came from, or ``None`` when running on the fallback.
        self.source = source

    @property
    def from_schema(self) -> bool:
        """Whether a real schema backs this, as opposed to the ``.glb`` fallback."""
        return self._pairs is not None

    def is_mesh_field(self, component_type: str | None, field: str, value: object) -> bool:
        """Whether this payload member names a mesh the loader should display."""
        if not isinstance(value, str) or not value:
            return False
        if self._pairs is not None and component_type is not None:
            return (component_type, field) in self._pairs
        return value.lower().endswith(".glb")


def load(project_root: str) -> MeshFields:
    """Read the game's schema dump, or return the fallback when there is none."""
    for candidate in _CANDIDATES:
        path = os.path.join(project_root, candidate.replace("/", os.sep))
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as handle:
                document = json.load(handle)
        except (OSError, json.JSONDecodeError):
            # An unreadable dump is not worth failing a scene load over: the fallback still
            # finds every .glb, and the panel reports which source is in use.
            continue

        pairs: set[tuple[str, str]] = set()
        for component in document.get("components", []):
            type_name = component.get("type")
            if isinstance(type_name, str):
                _collect(component.get("fields"), type_name, pairs)
        return MeshFields(pairs, path)

    return MeshFields(None, None)


def _collect(fields, type_name: str, pairs: set[tuple[str, str]]) -> None:
    """Top-level members only: the loader addresses mesh fields by name, not path."""
    if not isinstance(fields, list):
        return
    for entry in fields:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str) and entry.get("authoredBy") == "mesh":
            pairs.add((type_name, name))
