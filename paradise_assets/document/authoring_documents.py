"""Game-declared project documents, edited through the same fields as prefab components."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import PurePosixPath

from . import atomic, canonical_toml, component_schema
from .project import ProjectLayout

FILE_NAME = "authoring-documents.json"
_CACHE: dict[str, tuple[int, int, tuple]] = {}
_MISSING = object()


@dataclass(frozen=True)
class DocumentSchema:
    id: str
    display_name: str
    path: str
    component_id: str | None
    fields: tuple[dict, ...]

    def schema(self, vocabulary):
        from .component_schema import ComponentSchema

        if self.component_id:
            component = vocabulary.get(self.component_id)
            if component is None:
                raise ValueError(f"{self.display_name}: rebuild the game schema for {self.component_id}")
            return ComponentSchema(
                {
                    "id": self.id,
                    "displayName": self.display_name,
                    "fields": [_field_raw(field) for field in component.fields],
                }
            )
        return ComponentSchema({"id": self.id, "displayName": self.display_name, "fields": list(self.fields)})


def _field_raw(field) -> dict:
    """Component fields remain the game's source of truth; only the document identity differs."""
    raw = {
        "name": field.name,
        "type": field.type,
        "doc": field.doc,
        "default": field.default,
        "minimum": field.minimum,
        "maximum": field.maximum,
        "unit": field.unit,
        "authoredBy": field.authored_by,
        "lightField": field.light_field,
        "assetKinds": field.asset_kinds,
        "values": field.values,
        "fields": [_field_raw(child) for child in field.fields],
        "optional": field.optional,
    }
    if field.items is not None:
        raw["items"] = _field_raw(field.items)
    if field.visible_when_field:
        raw["visibleWhen"] = {"field": field.visible_when_field, "equals": field.visible_when_equals}
    return raw


def load(project_root: str) -> tuple[DocumentSchema, ...]:
    path = os.path.join(project_root, ".editor", FILE_NAME)
    try:
        stamp = os.stat(path)
    except FileNotFoundError:
        _CACHE.pop(path, None)
        return ()
    cached = _CACHE.get(path)
    if cached is not None and cached[:2] == (stamp.st_mtime_ns, stamp.st_size):
        return cached[2]
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError(f"{FILE_NAME}: expected version 1")
    documents = raw.get("documents")
    if not isinstance(documents, list):
        raise ValueError(f"{FILE_NAME}: documents must be a list")
    found = []
    ids = set()
    for entry in documents:
        if not isinstance(entry, dict):
            raise ValueError(f"{FILE_NAME}: each document must be an object")
        ident = entry.get("id")
        if not isinstance(ident, str) or not ident or ident in ids:
            raise ValueError(f"{FILE_NAME}: document ids must be nonempty and unique")
        ids.add(ident)
        relative = entry.get("path")
        _validate_path(relative)
        component = entry.get("componentId")
        fields = entry.get("fields")
        if component is not None:
            if not isinstance(component, str) or not component or fields is not None:
                raise ValueError(f"{ident}: declare either componentId or fields")
        elif not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields):
            raise ValueError(f"{ident}: fields must be a list")
        found.append(
            DocumentSchema(ident, entry.get("displayName") or ident, relative, component, tuple(fields or ()))
        )
    result = tuple(found)
    _CACHE[path] = (stamp.st_mtime_ns, stamp.st_size, result)
    return result


def _validate_path(path) -> None:
    if (
        not isinstance(path, str)
        or not path
        or "\\" in path
        or PurePosixPath(path).is_absolute()
        or ":" in path
        or any(part in ("", ".", "..") for part in path.split("/"))
        or not path.endswith(".toml")
    ):
        raise ValueError(f"Project document path must be an assets-relative TOML path: {path!r}")


def resolve(layout: ProjectLayout, document: DocumentSchema) -> str:
    _validate_path(document.path)
    root = os.path.realpath(layout.assets)
    path = os.path.realpath(os.path.join(root, *document.path.split("/")))
    if os.path.commonpath((root, path)) != root:
        raise ValueError(f"{document.display_name}: document resolves outside assets/")
    return path


def _payload(root: dict, document: DocumentSchema) -> dict:
    if document.component_id is None:
        return root
    components = root.get("Components")
    selected = (
        [
            component
            for component in components
            if isinstance(component, dict)
            and str(component.get("Id", "")).lower() == document.component_id.lower()
        ]
        if isinstance(components, list)
        else []
    )
    if len(selected) != 1 or not isinstance(selected[0].get("Data"), dict):
        raise ValueError(
            f"{document.path}: expected one Components entry for {document.component_id} with Data"
        )
    return selected[0]["Data"]


def read(layout: ProjectLayout, document: DocumentSchema) -> dict:
    with open(resolve(layout, document), encoding="utf-8") as handle:
        root = canonical_toml.loads(handle.read())
    return copy.deepcopy(_payload(root, document))


def save(
    layout: ProjectLayout, document: DocumentSchema, schema, fields: dict, *, baseline: dict | None = None
) -> dict:
    """Merge touched paths into the current file, preserving unrelated on-disk changes."""
    from .. import edits

    path = resolve(layout, document)
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    root = canonical_toml.loads(text)
    payload = _payload(root, document)
    prefix = ("Components", "Data") if document.component_id else ()
    header_arrays = set(canonical_toml.header_array_paths(text))
    for field in schema.fields:
        header_arrays.update(component_schema.array_header_paths(field, (*prefix, field.name)))
    if baseline is not None:
        for field_path in fields:
            if _conflicts(baseline, payload, field_path.split("/")):
                raise ValueError(
                    f"{document.path}: {field_path} changed on disk; reload settings before saving"
                )
    for field_path, value in fields.items():
        field = schema.resolve(field_path)
        if field is None:
            raise ValueError(
                f"{document.path}: field {field_path!r} is no longer in the game schema; reload it"
            )
        if value is None:
            if not field.optional:
                raise ValueError(f"{field_path}: only an optional field can be omitted")
            edits.drop_path(payload, field_path)
        else:
            field_location = (*prefix, *(part for part in field_path.split("/") if not part.isdigit()))
            restored = canonical_toml.restore_inline_tables(
                copy.deepcopy(value), header_arrays, field_location
            )
            edits.write_path(payload, field_path, restored)
    if fields:
        component_schema.omit_optional(payload, schema.fields)
        atomic.write_text(path, canonical_toml.dumps(root))
    return copy.deepcopy(payload)


def _conflicts(before, current, path: list[str]) -> bool:
    # Indices have no stable identity. Refuse any changed containing array instead of
    # accidentally applying a row edit to another item after an external reorder.
    if not path or isinstance(before, list) or isinstance(current, list):
        return before != current
    if before is _MISSING:
        before = {}
    if current is _MISSING:
        current = {}
    if not isinstance(before, dict) or not isinstance(current, dict):
        return before != current
    key, *rest = path
    return _conflicts(before.get(key, _MISSING), current.get(key, _MISSING), rest)
