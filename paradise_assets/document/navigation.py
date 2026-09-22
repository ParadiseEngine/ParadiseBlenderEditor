"""Generated navigation paths, shared by the panel and canonical document save."""

from __future__ import annotations

from pathlib import Path

from .. import edits


def asset_path(document_path: str, assets_root: str) -> str:
    """A level and its baked navigation share a directory and a stem."""
    try:
        relative = Path(document_path).resolve().relative_to(Path(assets_root).resolve())
    except ValueError as error:
        raise ValueError("The level must be inside the project's assets directory") from error
    return relative.with_suffix(".navmesh").as_posix()


def fields(schema, data):
    return [item.path for item in schema.plan(data) if item.field.authored_by == "navmesh"]


def normalize(document, vocabulary, document_path: str, assets_root: str) -> int:
    """Write only generated navigation fields; retain all other component payloads."""
    changed = 0
    for obj in document.objects:
        for component in obj.components:
            if component.removed:
                continue
            schema = vocabulary.get(component.id)
            if schema is None:
                continue
            for path in fields(schema, component.data):
                value = asset_path(document_path, assets_root)
                if edits.read_path(component.data, path) != value:
                    edits.write_path(component.data, path, value)
                    changed += 1
    return changed
