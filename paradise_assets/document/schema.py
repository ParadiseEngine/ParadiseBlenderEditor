"""Which component fields name a MESH, read from the dump's ``authoredBy: mesh``. The dump is
an enrichment, not a requirement: a fresh clone has none, so the fallback is "a string ending
in .glb". A wrong guess costs a wrong preview, not data, since payload writes never consult
this; that is what makes a heuristic acceptable here and nowhere else.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .project import SCHEMA_CANDIDATES

__all__ = ["MeshComponent", "MeshFields", "load", "mesh_components"]



class MeshFields:
    """Which ``(component type, field name)`` pairs hold a mesh reference."""

    def __init__(self, pairs: set[tuple[str, str]] | None, source: str | None) -> None:
        self._pairs = pairs
        #: Where the schema came from, or ``None`` when running on the fallback.
        self.source = source

    @property
    def from_schema(self) -> bool:
        """Whether a real schema backs this, as opposed to the suffix fallback."""
        return self._pairs is not None

    def is_mesh_field(self, component_type: str | None, field: str, value: object) -> bool:
        """Whether this payload member names a mesh the loader should display."""
        if not isinstance(value, str) or not value:
            return False
        if self._pairs is not None and component_type is not None:
            return (component_type, field) in self._pairs
        return value.lower().endswith((".glb", ".mesh", ".skinnedmesh"))


@dataclass(frozen=True)
class MeshComponent:
    """A component that carries a mesh reference, named completely enough to AUTHOR one.

    :class:`MeshFields` answers "is this field a mesh?" for a payload that already exists; a
    generator has to write the component from nothing, so it needs the id and the CLR type name
    as well. The game decides how many of these it declares -- ShiningPie has two -- so nothing
    here may guess which one a generated prefab should use.
    """

    component_id: str
    type_name: str
    field_name: str


def load(project_root: str) -> MeshFields:
    """Read the game's schema dump, or return the fallback when there is none."""
    document, path = _read_dump(project_root)
    if document is None:
        return MeshFields(None, None)

    pairs: set[tuple[str, str]] = set()
    for component in document.get("components", []):
        type_name = component.get("type")
        if isinstance(type_name, str):
            _collect(component.get("fields"), type_name, pairs)
    return MeshFields(pairs, path)


def mesh_components(project_root: str) -> list[MeshComponent]:
    """Every mesh-bearing component the game declares, by display order of its type name."""
    document, _ = _read_dump(project_root)
    if document is None:
        return []

    found: list[MeshComponent] = []
    for component in document.get("components", []):
        if not isinstance(component, dict):
            continue
        component_id = component.get("id")
        type_name = component.get("type")
        if not isinstance(component_id, str) or not isinstance(type_name, str):
            continue
        for entry in component.get("fields") or []:
            if not isinstance(entry, dict) or entry.get("authoredBy") != "mesh":
                continue
            name = entry.get("name")
            if isinstance(name, str):
                found.append(MeshComponent(component_id, type_name, name))
    found.sort(key=lambda item: (item.type_name.lower(), item.field_name))
    return found


#: path -> (mtime_ns, size, document). The panel asks per redraw and the dump is ~40 KB of
#: JSON, so parsing it per frame was the redraw's cost; a rebuild changes the stamp.
_CACHE: dict[str, tuple[int, int, dict]] = {}


def _read_dump(project_root: str) -> tuple[dict | None, str | None]:
    """The game's schema dump and where it was read from, or ``(None, None)``."""
    for candidate in SCHEMA_CANDIDATES:
        path = os.path.join(project_root, candidate.replace("/", os.sep))
        try:
            stat = os.stat(path)
        except OSError:
            continue

        cached = _CACHE.get(path)
        if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return cached[2], path

        try:
            with open(path, "rb") as handle:
                document = json.load(handle)
        except (OSError, json.JSONDecodeError):
            # An unreadable dump is not worth failing a scene load over: the fallback still
            # finds every .glb, and the panel reports which source is in use.
            continue
        if isinstance(document, dict):
            _CACHE[path] = (stat.st_mtime_ns, stat.st_size, document)
            return document, path
    return None, None


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
