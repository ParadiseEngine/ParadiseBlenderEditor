"""Python mirror of ``Paradise.Authoring``'s schema document, and the payload builder.

A game declares components once, as C# records marked ``[Authored]``; a Roslyn generator dumps
their description to ``<data>/authoring-schema.json``. This module is how the Blender host reads
that description and turns authored values back into the JSON payloads the runtime deserializes
-- the same two halves the Godot host implements in ``AuthoredEntityCore`` (``LoadSchema`` and
``ExportAuthoredComponents``). When this module and that class disagree, that class is right:
it defined the wire format the engine's generated readers were written against.

The wire format, stated once (reference: ``AuthoredEntityCore.ValueOf``):

* Every plain field of an enabled component is written, defaults filling anything unset --
  the reader keeps a record initializer only for a key that is *absent*, and an editor that
  omitted unset fields would silently pin them to C#-side defaults it cannot see.
* An empty string with **no declared default** is written as ``null``: the record had no
  initializer, so its own default is null and the contract preserves that. A field that
  declared ``""`` keeps writing ``""``.
* Enums travel by member **name** (``"Chase"``), exactly the string the schema's ``values``
  lists -- matching the typed contract's ``JsonStringEnumConverter`` spelling.
* ``vector2``/``vector3``/``quaternion`` are flat float arrays; ``color`` is ``{r,g,b,a}``
  floats.
* Composition is a tree: the schema flattens to slash paths (``"Box/SizeX"``) for editing, and
  the payload builder re-nests them. Path and tree cannot disagree because one is derived from
  the other.
* Fields (or whole components) with ``authoredBy`` are host-object *references* that the Godot
  host bakes into values at export. This host does not bake any of them yet; such fields are
  skipped, which the reader treats as unauthored. :func:`flatten` reports them so the UI can
  say so instead of drawing a control that exports nothing.

No ``bpy`` import: this module is pure data and is unit-tested standalone.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

__all__ = [
    "CURRENT_VERSION",
    "MINIMUM_SUPPORTED_VERSION",
    "SCHEMA_FILE_NAME",
    "AuthoredComponentSchema",
    "AuthoredFieldSchema",
    "AuthoredGizmoSchema",
    "AuthoredVisibilitySchema",
    "AuthoringSchemaDocument",
    "FlatField",
    "HostRef",
    "SchemaError",
    "build_payload",
    "default_of",
    "flatten",
    "merge",
    "read",
    "read_engine_schema",
    "schema_path",
    "schema_stamp",
]

# AuthoringSchemaDocument.CurrentVersion / MinimumSupportedVersion. v2 added arrays, vectors,
# quaternions, colour, conditional visibility, and host-object references beyond collision
# shapes. Bump only in lockstep with the engine.
CURRENT_VERSION = 2
MINIMUM_SUPPORTED_VERSION = 1

SCHEMA_FILE_NAME = "authoring-schema.json"

# AuthoredFieldTypes -- the closed set. Anything else in a document is a schema from a newer
# engine than this reader, which the version gate should have caught first.
TYPE_FLOAT = "float"
TYPE_INT = "int"
TYPE_BOOL = "bool"
TYPE_STRING = "string"
TYPE_ENUM = "enum"
TYPE_OBJECT = "object"
TYPE_ARRAY = "array"
TYPE_VECTOR2 = "vector2"
TYPE_VECTOR3 = "vector3"
TYPE_QUATERNION = "quaternion"
TYPE_COLOR = "color"

# AuthoredBySources: v1 spelled the only host-object kind "nativeShape"; normalized on read so
# every consumer sees one vocabulary, exactly as AuthoringSchemaReader does.
SOURCE_SHAPE = "shape"
_SOURCE_NATIVE_SHAPE = "nativeShape"


class SchemaError(ValueError):
    """A document this reader cannot use. Raised loudly on purpose: the symptom of a silently
    skipped schema is "my component vanished from the dropdown" with no cause anywhere."""


@dataclass
class AuthoredVisibilitySchema:
    """Show a field only while a sibling holds a given value."""

    field: str = ""
    equals: Any = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AuthoredVisibilitySchema:
        return cls(field=data.get("field", ""), equals=data.get("equals"))


@dataclass
class AuthoredGizmoSchema:
    """How to draw the component while editing. Currently only ``box``."""

    kind: str = ""
    half_extent_x: str | None = None
    half_extent_z: str | None = None
    depth: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AuthoredGizmoSchema:
        return cls(
            kind=data.get("kind", ""),
            half_extent_x=data.get("halfExtentX"),
            half_extent_z=data.get("halfExtentZ"),
            depth=data.get("depth"),
        )


@dataclass
class AuthoredFieldSchema:
    """One editable value. Recursive: a composed field carries its own ``fields``."""

    name: str = ""
    type: str = ""
    unit: str | None = None
    doc: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    default: Any = None
    has_default: bool = False
    values: list[str] | None = None
    authored_by: str | None = None
    fields: list[AuthoredFieldSchema] | None = None
    items: AuthoredFieldSchema | None = None
    visible_when: AuthoredVisibilitySchema | None = None
    asset_kinds: list[str] | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AuthoredFieldSchema:
        authored_by = data.get("authoredBy")
        if authored_by == _SOURCE_NATIVE_SHAPE:
            authored_by = SOURCE_SHAPE
        return cls(
            name=data.get("name", ""),
            type=data.get("type", ""),
            unit=data.get("unit"),
            doc=data.get("doc"),
            minimum=data.get("minimum"),
            maximum=data.get("maximum"),
            default=data.get("default"),
            # JSON null and JSON absent are the same thing to Python's .get, but the contract
            # distinguishes "declared ''" from "no initializer" -- key presence is the tell.
            has_default="default" in data and data["default"] is not None,
            values=list(data["values"]) if data.get("values") else None,
            authored_by=authored_by,
            fields=[cls.from_json(f) for f in data["fields"]] if data.get("fields") else None,
            items=cls.from_json(data["items"]) if data.get("items") else None,
            visible_when=(
                AuthoredVisibilitySchema.from_json(data["visibleWhen"])
                if data.get("visibleWhen")
                else None
            ),
            asset_kinds=list(data["assetKinds"]) if data.get("assetKinds") else None,
        )


@dataclass
class AuthoredComponentSchema:
    """One authored component: the id it travels under, and the fields a human edits."""

    id: str = ""
    display_name: str = ""
    gizmo: AuthoredGizmoSchema | None = None
    authored_by: str | None = None
    fields: list[AuthoredFieldSchema] = dataclass_field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AuthoredComponentSchema:
        component_id = data.get("id", "")
        return cls(
            id=component_id,
            display_name=data.get("displayName") or component_id,
            gizmo=AuthoredGizmoSchema.from_json(data["gizmo"]) if data.get("gizmo") else None,
            authored_by=data.get("authoredBy"),
            fields=[AuthoredFieldSchema.from_json(f) for f in data.get("fields") or []],
        )


@dataclass
class AuthoringSchemaDocument:
    version: int = CURRENT_VERSION
    components: list[AuthoredComponentSchema] = dataclass_field(default_factory=list)


def read_engine_schema() -> AuthoringSchemaDocument:
    """The ENGINE's own component schema — what `Paradise.Export.AuthoringSchema.Json` holds.

    Vendored as a JSON file beside this module because the constant lives in a C# assembly this
    Python host cannot load. The copy is kept honest by the conformance suite: the bridge's
    ``engine-schema`` verb prints the constant from the real Paradise.Export, and
    ``tools/run_tests.sh`` fails if the two differ. Regenerate with::

        dotnet run --project tools/ParadiseBlenderBridge -- engine-schema \
            > paradise_blender/contract/engine_authoring_schema.json
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine_authoring_schema.json")
    with open(path, encoding="utf-8") as file:
        return read(file.read())


def read(text: str) -> AuthoringSchemaDocument:
    """Parse one document, mirroring ``AuthoringSchemaReader.Read`` -- including its version
    gate, which is what keeps a newer engine's schema from being half-understood."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise SchemaError(f"Authoring schema is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise SchemaError("Authoring schema document must be a JSON object.")

    version = data.get("version", CURRENT_VERSION)
    if version > CURRENT_VERSION:
        raise SchemaError(
            f"Authoring schema is version {version}, but this addon understands at most "
            f"{CURRENT_VERSION}. Update the addon."
        )
    if version < MINIMUM_SUPPORTED_VERSION:
        raise SchemaError(
            f"Authoring schema is version {version}, older than the minimum supported "
            f"{MINIMUM_SUPPORTED_VERSION}. Regenerate it."
        )

    return AuthoringSchemaDocument(
        version=version,
        components=[AuthoredComponentSchema.from_json(c) for c in data.get("components") or []],
    )


def merge(documents: list[AuthoringSchemaDocument]) -> AuthoringSchemaDocument:
    """Combine documents into one, earlier sources winning on a duplicate id and components
    ordered by id -- ``AuthoringSchemaReader.Merge``. Earlier-wins so a host can pass the
    engine's schema first and have it be authoritative."""
    by_id: dict[str, AuthoredComponentSchema] = {}
    for document in documents:
        for component in document.components:
            if component.id and component.id not in by_id:
                by_id[component.id] = component
    return AuthoringSchemaDocument(components=[by_id[key] for key in sorted(by_id)])


def schema_path(data_dir: str) -> str:
    """Where a game's dumped schema lives -- ``<data>/authoring-schema.json``, matching the
    Godot host's ``ParadisePaths.DataDirPrefix + SchemaFileName``."""
    return os.path.join(data_dir, SCHEMA_FILE_NAME)


def schema_stamp(path: str) -> tuple[int, int]:
    """A cheap change detector for hot reload: (mtime_ns, size). Size is in the stamp because
    mtime granularity can be coarse enough for a rebuild to land inside one tick -- the same
    reason the Godot host hashes length into its stamp."""
    try:
        stat = os.stat(path)
    except OSError:
        return (0, 0)
    return (stat.st_mtime_ns, stat.st_size)


# --------------------------------------------------------------------------------------
# Flattening -- the schema's field tree as editable slash paths
# --------------------------------------------------------------------------------------


@dataclass
class FlatField:
    """One editable leaf, addressed by its slash path (``"Box/SizeX"``)."""

    path: str
    type: str
    unit: str | None = None
    doc: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    values: list[str] | None = None
    visible_when: AuthoredVisibilitySchema | None = None
    default: Any = None
    has_default: bool = False
    asset_kinds: list[str] | None = None


@dataclass
class HostRef:
    """A field this host cannot author yet: a reference to a host object (a shape, an asset,
    a node) that the Godot host bakes into values at export. Reported so the UI can say what
    is missing and why, instead of silently exporting a component with holes."""

    path: str
    kind: str
    is_list: bool = False


def flatten(component: AuthoredComponentSchema) -> tuple[list[FlatField], list[HostRef]]:
    """The component's field tree as leaf paths, plus the host references it wants baked.

    Mirrors ``AuthoredEntityCore.ReadFields``: composed fields recurse with a path prefix,
    ``authoredBy`` fields become host references rather than editable leaves, and an array is
    only a host-reference list (a list of typed rows has no author asking for it yet).
    """
    fields: list[FlatField] = []
    hosts: list[HostRef] = []
    _flatten_into(component.fields, "", fields, hosts)
    return fields, hosts


def _flatten_into(
    source: list[AuthoredFieldSchema],
    prefix: str,
    fields: list[FlatField],
    hosts: list[HostRef],
) -> None:
    for field in source:
        path = prefix + field.name

        if field.type == TYPE_ARRAY:
            if field.items is not None and field.items.authored_by is not None:
                hosts.append(HostRef(path=path, kind=field.items.authored_by, is_list=True))
            else:
                hosts.append(HostRef(path=path, kind="rows", is_list=True))
            continue

        if field.authored_by is not None:
            hosts.append(HostRef(path=path, kind=field.authored_by))
            continue

        if field.fields:
            _flatten_into(field.fields, path + "/", fields, hosts)
            continue

        fields.append(
            FlatField(
                path=path,
                type=field.type,
                unit=field.unit,
                doc=field.doc,
                minimum=field.minimum,
                maximum=field.maximum,
                values=field.values,
                visible_when=field.visible_when,
                default=default_of(field),
                has_default=field.has_default,
                asset_kinds=field.asset_kinds,
            )
        )


def default_of(field: AuthoredFieldSchema) -> Any:
    """A field's default, read AT ITS SCHEMA TYPE -- a bool for a bool, never everything as a
    number. An enum with no declared default still starts on a legal member, or the dropdown
    would open on a value the runtime cannot parse."""
    declared = field.default
    if field.type == TYPE_BOOL:
        return bool(declared) if isinstance(declared, bool) else False
    if field.type == TYPE_INT:
        return int(declared) if _is_number(declared) else 0
    if field.type == TYPE_FLOAT:
        return float(declared) if _is_number(declared) else 0.0
    if field.type in (TYPE_STRING, TYPE_ENUM):
        if isinstance(declared, str):
            return declared
        return field.values[0] if field.values else ""
    if field.type in (TYPE_VECTOR2, TYPE_VECTOR3, TYPE_QUATERNION):
        size = {TYPE_VECTOR2: 2, TYPE_VECTOR3: 3, TYPE_QUATERNION: 4}[field.type]
        if isinstance(declared, list) and len(declared) == size:
            return [float(v) for v in declared]
        # A zero quaternion is not a rotation; identity is the only sane unset value.
        return [0.0, 0.0, 0.0, 1.0] if field.type == TYPE_QUATERNION else [0.0] * size
    if field.type == TYPE_COLOR:
        if isinstance(declared, dict):
            return [float(declared.get(c, 0.0)) for c in ("r", "g", "b")] + [
                float(declared.get("a", 1.0))
            ]
        return [0.0, 0.0, 0.0, 1.0]
    # An unknown leaf type from a same-version schema; treat as a number rather than dropping
    # the field, matching the Godot host's fallback.
    return float(declared) if _is_number(declared) else 0.0


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --------------------------------------------------------------------------------------
# Payload -- authored values back to the JSON the runtime deserializes
# --------------------------------------------------------------------------------------


def build_payload(
    component: AuthoredComponentSchema, values: Mapping[str, Any]
) -> dict[str, Any]:
    """The component's exported ``Data`` payload, from a flat ``{path: value}`` mapping.

    Every plain field is written at its schema type, defaults filling anything ``values`` does
    not carry -- see the module docstring for the wire rules. Host references are skipped: an
    absent key is "unauthored" to the reader, which is the truthful description of a bake this
    host does not perform.
    """
    fields, _ = flatten(component)
    payload: dict[str, Any] = {}
    for field in fields:
        _write_path(payload, field.path, _wire_value(field, values.get(field.path, field.default)))
    return payload


def _wire_value(field: FlatField, value: Any) -> Any:
    if field.type == TYPE_BOOL:
        return bool(value)
    if field.type == TYPE_INT:
        return int(value)
    if field.type in (TYPE_STRING, TYPE_ENUM):
        text = str(value) if value is not None else ""
        if field.type == TYPE_ENUM:
            # Never export a name outside the schema's own list -- a typo here is a runtime
            # parse error in the game, with this entity's name nowhere in the message.
            if field.values and text not in field.values:
                return field.values[0]
            return text
        # An empty string with no declared default is ABSENT, not empty: the record that
        # produced this field had no initializer, so its own default is null.
        if text == "" and not field.has_default:
            return None
        return text
    if field.type in (TYPE_VECTOR2, TYPE_VECTOR3, TYPE_QUATERNION):
        size = {TYPE_VECTOR2: 2, TYPE_VECTOR3: 3, TYPE_QUATERNION: 4}[field.type]
        floats = [float(v) for v in value] if isinstance(value, (list, tuple)) else []
        if len(floats) != size:
            floats = ([0.0, 0.0, 0.0, 1.0] if field.type == TYPE_QUATERNION else [0.0] * size)
        return floats
    if field.type == TYPE_COLOR:
        floats = [float(v) for v in value] if isinstance(value, (list, tuple)) else []
        if len(floats) == 3:
            floats.append(1.0)
        if len(floats) != 4:
            floats = [0.0, 0.0, 0.0, 1.0]
        return {"r": floats[0], "g": floats[1], "b": floats[2], "a": floats[3]}
    return float(value)


def _write_path(root: dict[str, Any], path: str, value: Any) -> None:
    """Write a slash path into a nested object, creating groups as needed -- the inverse of
    the flattening that produced the path."""
    parts = path.split("/")
    target = root
    for part in parts[:-1]:
        nested = target.get(part)
        if not isinstance(nested, dict):
            nested = {}
            target[part] = nested
        target = nested
    target[parts[-1]] = value
