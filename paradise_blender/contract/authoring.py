"""Python mirror of ``Paradise.Authoring``'s schema document, and the payload builder.

The reference implementation is the Godot host's ``AuthoredEntityCore`` (``LoadSchema``,
``ExportAuthoredComponents``, ``ValueOf``); when the two disagree, that class is right, because
the engine's generated readers were written against it. The wire rules that are easy to break:

* Every plain field is written, defaults filling anything unset: the reader keeps a record
  initializer only for an ABSENT key, so omitting unset fields would pin them to C# defaults
  this host cannot see.
* An empty string with no declared default is ``null`` (the record had no initializer);
  a field that declared ``""`` keeps ``""``.
* Enums travel by member name, the ``JsonStringEnumConverter`` spelling.
* A list row is an index segment in the same slash-path grammar (``Tables/0/Entries/1/Weight``).
  The schema says a member IS a list and nothing about its length, so row counts are DATA
  supplied by the caller (:func:`outline`, :func:`counts_of`).
* An ``authoredBy`` list is never editable as rows: a row editor over a collider's shapes would
  be a second, lying copy of the pointer list the entity already holds.

No ``bpy`` import: unit-tested standalone.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from .. import log

__all__ = [
    "COUNT_SUFFIX",
    "CURRENT_VERSION",
    "MAX_ROWS",
    "MINIMUM_SUPPORTED_VERSION",
    "SCHEMA_FILE_NAME",
    "AuthoredComponentSchema",
    "AuthoredFieldSchema",
    "AuthoredGizmoSchema",
    "AuthoredVisibilitySchema",
    "AuthoringSchemaDocument",
    "FlatArray",
    "FlatField",
    "FlatOutline",
    "HostRef",
    "SchemaError",
    "build_payload",
    "counts_of",
    "default_of",
    "field_caption",
    "flatten",
    "has_slider",
    "id_subtype",
    "merge",
    "numeric_widget_options",
    "outline",
    "read",
    "relative_to",
    "removal_mapping",
    "renumber",
    "row_container_of",
    "row_index_of",
    "schema_for_data_dir",
    "schema_load_error",
    "schema_path",
    "schema_stamp",
    "swap_mapping",
    "value_at",
]

# Lockstep with AuthoringSchemaDocument.CurrentVersion / MinimumSupportedVersion. The minimum
# EQUALS the current on purpose: v3 made `id` a GUID, and a v1/v2 document keyed by name cannot
# be upgraded on read, only regenerated -- refusing it names the problem.
CURRENT_VERSION = 3
MINIMUM_SUPPORTED_VERSION = 3

SCHEMA_FILE_NAME = "authoring-schema.json"

# AuthoredFieldTypes, the closed set.
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

#: Schema ``unit`` -> Blender ID-property subtype. Kilograms is absent because ID properties
#: have no MASS subtype, so the caption carries ``kg`` instead.
_SUBTYPE_FOR_UNIT = {
    "meters": "DISTANCE",
    "radians": "ANGLE",
    "seconds": "TIME",
    "unit01": "FACTOR",
}

_SHORT_UNIT = {
    "kilograms": "kg",
}


def id_subtype(unit: str | None) -> str | None:
    """Blender ID-property subtype for a schema unit, or None when the widget cannot carry it."""
    return _SUBTYPE_FOR_UNIT.get(unit) if unit else None


def field_caption(name: str, unit: str | None) -> str:
    """The label for a number field: units the widget already displays stay off it."""
    if not unit or unit in _SUBTYPE_FOR_UNIT:
        return name
    return f"{name} ({_SHORT_UNIT.get(unit, unit)})"


def has_slider(field: FlatField) -> bool:
    """Whether both ends of a range exist (or the unit is unit01), so the draw can cap a slider."""
    if field.unit == "unit01":
        return True
    return field.minimum is not None and field.maximum is not None


def numeric_widget_options(field: FlatField) -> dict[str, object]:
    """ID-property UI metadata for a float/int: range plus the unit's Blender subtype."""
    if field.type not in (TYPE_FLOAT, TYPE_INT):
        return {}
    options: dict[str, object] = {}
    if field.minimum is not None:
        options["min"] = field.minimum
    if field.maximum is not None:
        options["max"] = field.maximum
    subtype = id_subtype(field.unit)
    if subtype:
        options["subtype"] = subtype
    if field.unit == "unit01":
        options.setdefault("min", 0.0)
        options.setdefault("max", 1.0)
        options["subtype"] = "FACTOR"
    return options


#: ``authoredBy`` kinds; the closed set is ``Paradise.Authoring``'s ``AuthoredBySources``.
#: Record kinds fill the leaves a record declares under the reference.
HOST_TRANSFORM = "transform"
HOST_SHAPE = "shape"
HOST_LIGHT = "light"
HOST_CAMERA = "camera"

#: Leaf kinds: the reference IS the value. A mesh slot usually points at the object itself; the
#: slot exists so that "this draws" is something an author says rather than something an
#: exporter infers from the object having mesh data.
HOST_MESH = "mesh"
HOST_SPRITE = "sprite"
#: Baked as the target's identity, the same value its ``meta.Guid`` carries.
HOST_ENTITY = "entity"
#: A file browser over ``data/``, filtered by the field's declared extensions.
HOST_ASSET = "asset"

#: Self kinds: no picker, read off the entity being exported.
HOST_ID = "id"
HOST_NAME = "name"
HOST_PARENT = "parent"
HOST_LOCAL_POSITION = "local-position"
HOST_LOCAL_ROTATION = "local-rotation"
HOST_LOCAL_SCALE = "local-scale"

HOST_RECORD_KINDS = (HOST_TRANSFORM, HOST_SHAPE, HOST_LIGHT, HOST_CAMERA)
HOST_LEAF_KINDS = (HOST_MESH, HOST_ENTITY, HOST_ASSET, HOST_SPRITE)
HOST_SELF_KINDS = (
    HOST_ID,
    HOST_NAME,
    HOST_PARENT,
    HOST_LOCAL_POSITION,
    HOST_LOCAL_ROTATION,
    HOST_LOCAL_SCALE,
)

#: A kind outside this set is still reported, so the panel can say what is missing instead of
#: drawing a control that exports nothing.
HOST_IMPLEMENTED_KINDS = HOST_RECORD_KINDS + HOST_LEAF_KINDS + HOST_SELF_KINDS

#: Storage path of a component-level host reference; matches the Godot host's ``/Source``.
HOST_SOURCE_PATH = "Source"

#: The pose leaves a ``transform`` reference may fill. A record declares the parts it means and
#: the exporter ignores the rest, so the host never learns what a particular record means by a pose.
TRANSFORM_FIELDS = ("Position", "Rotation", "Yaw", "Scale")

SOURCE_SHAPE = "shape"

#: A clamp, not a policy: a row count arrives from a hand-editable store or a game-written file,
#: and a ``draw()`` looping a billion times hangs Blender with no way back to the fixing button.
MAX_ROWS = 4096

#: Suffix of a stored row-count key (``Tables#``, ``Tables/0/Entries#``). Known here only so
#: :func:`renumber` can carry a nested list's count along when its row moves. ONE character
#: because of the 63-char ID-property key budget (see ``config_store``); ``#`` cannot occur in a
#: C# member name or in base64url, so no field path or component token can produce it.
COUNT_SUFFIX = "#"


class SchemaError(ValueError):
    """Raised loudly on purpose: a silently skipped schema presents as "my component vanished
    from the dropdown" with no cause anywhere."""


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
        return cls(
            name=data.get("name", ""),
            type=data.get("type", ""),
            unit=data.get("unit"),
            doc=data.get("doc"),
            minimum=data.get("minimum"),
            maximum=data.get("maximum"),
            default=data.get("default"),
            # The contract distinguishes "declared ''" from "no initializer"; key presence is
            # the tell, and .get() would erase it.
            has_default="default" in data and data["default"] is not None,
            values=list(data["values"]) if data.get("values") else None,
            authored_by=data.get("authoredBy"),
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

    #: Canonical lowercase-hyphenated GUID; the only member anything may match on.
    id: str = ""

    #: Fully qualified CLR name. Copied verbatim onto the payload, never synthesized: the
    #: engine's type-name fallback is an exact ordinal match.
    type: str = ""

    display_name: str = ""
    gizmo: AuthoredGizmoSchema | None = None
    authored_by: str | None = None
    fields: list[AuthoredFieldSchema] = dataclass_field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AuthoredComponentSchema:
        # Lowercased here (AuthoredModel's `parsed.ToString("D")`): a hand-typed uppercase [Guid]
        # would otherwise open a second storage namespace on the same object.
        component_id = data.get("id", "").strip().lower()
        component_type = data.get("type", "")
        return cls(
            id=component_id,
            type=component_type,
            display_name=data.get("displayName") or component_type or component_id,
            gizmo=AuthoredGizmoSchema.from_json(data["gizmo"]) if data.get("gizmo") else None,
            authored_by=data.get("authoredBy"),
            fields=[AuthoredFieldSchema.from_json(f) for f in data.get("fields") or []],
        )


@dataclass
class AuthoringSchemaDocument:
    version: int = CURRENT_VERSION
    components: list[AuthoredComponentSchema] = dataclass_field(default_factory=list)


def read(text: str) -> AuthoringSchemaDocument:
    """Parse one document with ``AuthoringSchemaReader.Read``'s version gate."""
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
    """``AuthoringSchemaReader.Merge``: earlier documents win on a duplicate id.

    Ordered by type, not id: an id is a GUID, and a GUID order is one no reader can predict,
    while the panel draws and the exporter writes in this order.
    """
    by_id: dict[str, AuthoredComponentSchema] = {}
    for document in documents:
        for component in document.components:
            if component.id and component.id not in by_id:
                by_id[component.id] = component
    return AuthoringSchemaDocument(
        components=sorted(by_id.values(), key=lambda component: (component.type, component.id))
    )


# The schema cache lives in the bpy-free layer because export (component_ids.engine_type_name)
# reads it and contract/ may not import bpy. Keyed per data directory: two projects can be open
# in one Blender session.
_cache: dict[str, tuple[tuple[int, int], AuthoringSchemaDocument, str | None]] = {}


def schema_for_data_dir(data_dir: str) -> AuthoringSchemaDocument:
    """The game's schema, re-read when the file's stamp changes.

    One document, the game's. A vendored engine schema used to be merged underneath it; since a
    launcher built with ``ParadiseAuthoringScanReferences`` dumps the engine's components too,
    that copy was a hazard (merges are first-wins, so a drifted copy beat the truth). The cost:
    with no dump there is no floor, and the panel is empty until the game is built once, which
    :func:`schema_load_error` explains. Unreadable yields empty rather than raising, so a
    ``draw()`` never dies over it.
    """
    path = schema_path(data_dir)
    stamp = schema_stamp(path)
    cached = _cache.get(data_dir)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    document = AuthoringSchemaDocument()
    error: str | None = None
    if stamp == (0, 0):
        error = (
            f"No '{SCHEMA_FILE_NAME}' in '{data_dir}'. Build the game's LAUNCHER to "
            "dump it — it is the project that references the whole game, so its dump is the one "
            "that describes all of it (engine components included)."
        )
    else:
        try:
            with open(path, encoding="utf-8") as file:
                document = read(file.read())
        except (OSError, SchemaError) as failure:
            error = f"'{path}' is not a readable authoring schema: {failure}"
            log.warn(error)

    _cache[data_dir] = (stamp, document, error)
    return document


def schema_load_error(data_dir: str) -> str | None:
    """Why the schema for this directory is empty, or None when it loaded."""
    schema_for_data_dir(data_dir)  # ensure the cache entry reflects the current stamp
    cached = _cache.get(data_dir)
    return cached[2] if cached is not None else None


def schema_path(data_dir: str) -> str:
    """``<data>/authoring-schema.json``, where the Godot host also looks."""
    return os.path.join(data_dir, SCHEMA_FILE_NAME)


def schema_stamp(path: str) -> tuple[int, int]:
    """(mtime_ns, size). Size is included because a rebuild can land inside one mtime tick."""
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
    """A field authored by referencing a host object rather than by typing its numbers.

    ``leaves`` holds the SCHEMAS the reference fills, captured during the walk where the parent
    field is in hand. Re-deriving them afterwards by matching the ref's path against the
    component's top-level fields worked only for a top-level reference: a nested one
    (``Container/Destination``) matched nothing, its leaves came back empty, and the pose baked
    correctly and was then silently dropped from the payload.
    """

    path: str
    kind: str
    is_list: bool = False
    leaves: tuple[AuthoredFieldSchema, ...] = ()

    #: Set for a leaf or self reference (the reference IS the value, written at ``path``);
    #: None for a record reference. This is what tells the two apart.
    leaf_type: str | None = None

    asset_kinds: tuple[str, ...] = ()

    @property
    def bakes(self) -> tuple[str, ...]:
        """The leaf names."""
        return tuple(leaf.name for leaf in self.leaves)

    @property
    def is_authorable(self) -> bool:
        """Whether this host can author the reference rather than only report it.

        A list of references never is: a row editor over a pointer list would be a second,
        lying copy of the list the entity already holds.
        """
        if self.is_list:
            return False
        if self.kind in HOST_SELF_KINDS or self.kind in HOST_LEAF_KINDS:
            return self.leaf_type is not None
        return self.kind in HOST_RECORD_KINDS and bool(self.leaves)

    @property
    def stores_slot(self) -> bool:
        """Whether enabling the component writes a picker key. Self kinds bake from THIS
        object and must not grow a stored name that can disagree with it."""
        return self.is_authorable and self.kind not in HOST_SELF_KINDS


@dataclass
class FlatArray:
    """One authored list, addressed by its INSTANCE path (``Tables/0/Entries``), since two
    rows' nested lists hold different counts and no schema path can say so. Reported apart from
    fields because an empty list has no leaves yet still needs a header and an Add button."""

    path: str
    label: str
    count: int
    rows_are_records: bool
    #: First string-ish leaf of a row, relative to it, so a panel can title rows by content.
    row_title_path: str | None = None
    doc: str | None = None


@dataclass
class FlatOutline:
    """``sequence`` interleaves leaves and lists in declaration order; ``fields``/``arrays`` are
    views of it. Interleaving keeps payload keys in schema order: seeding lists first would hoist
    each row's nested list above its siblings and turn a no-op save into a whole-file diff."""

    sequence: list[FlatField | FlatArray] = dataclass_field(default_factory=list)
    hosts: list[HostRef] = dataclass_field(default_factory=list)
    fields: list[FlatField] = dataclass_field(default_factory=list)
    arrays: list[FlatArray] = dataclass_field(default_factory=list)


def outline(
    component: AuthoredComponentSchema, counts: Mapping[str, int] | None = None
) -> FlatOutline:
    """The field tree as leaf paths, host references, and lists expanded to ``counts`` rows.

    Mirrors ``AuthoredEntityCore.ReadFields``. ``counts`` maps an instance path to a row count
    (:func:`counts_of` from a payload, ``config_store.counts_for_store`` from a store); absent
    means every list is empty. ``arrays`` comes out parent-before-child, which
    :func:`build_payload` relies on: a nested list can only be created once its row exists.
    """
    plan = FlatOutline()
    _walk(component.fields, "", counts or {}, plan)
    plan.fields = [item for item in plan.sequence if isinstance(item, FlatField)]
    plan.arrays = [item for item in plan.sequence if isinstance(item, FlatArray)]
    return plan


def flatten(
    component: AuthoredComponentSchema, counts: Mapping[str, int] | None = None
) -> tuple[list[FlatField], list[HostRef]]:
    """The (fields, hosts) facade over :func:`outline`, kept so call sites that need no arrays stay put."""
    plan = outline(component, counts)
    return plan.fields, plan.hosts


def _walk(
    source: list[AuthoredFieldSchema],
    prefix: str,
    counts: Mapping[str, int],
    plan: FlatOutline,
) -> None:
    for field in source:
        _walk_field(field, prefix + field.name, counts, plan)


def _walk_field(
    field: AuthoredFieldSchema, path: str, counts: Mapping[str, int], plan: FlatOutline
) -> None:
    """One field at an already-built path.

    The path is a parameter rather than derived from ``field.name`` so that an array element
    (whose schema has an empty name) takes the same branch as a named field; lists of records,
    scalars and lists then share one code path.
    """
    if field.type == TYPE_ARRAY:
        items = field.items
        if items is None or items.authored_by is not None:
            plan.hosts.append(
                HostRef(path=path, kind=items.authored_by if items else "rows", is_list=True)
            )
            return
        count = _row_count(counts, path)
        plan.sequence.append(
            FlatArray(
                path=path,
                label=field.name or path.rsplit("/", 1)[-1],
                count=count,
                rows_are_records=bool(items.fields),
                row_title_path=_title_path(items),
                doc=field.doc,
            )
        )
        for index in range(count):
            _walk_field(items, f"{path}/{index}", counts, plan)
        return

    if field.authored_by is not None:
        # A pose has a closed vocabulary (TRANSFORM_FIELDS); a record may carry other leaves a
        # host would only be inventing meaning for. Shape/light/camera are the other way round:
        # the exporter bakes a whole record and the referencing record says which parts it means.
        children = tuple(field.fields or ())
        if field.authored_by == HOST_TRANSFORM:
            leaves = tuple(child for child in children if child.name in TRANSFORM_FIELDS)
        elif field.authored_by in HOST_RECORD_KINDS:
            leaves = children
        else:
            leaves = ()
        plan.hosts.append(HostRef(
            path=path,
            kind=field.authored_by,
            leaves=leaves,
            leaf_type=(
                field.type
                if field.authored_by in HOST_LEAF_KINDS or field.authored_by in HOST_SELF_KINDS
                else None
            ),
            asset_kinds=tuple(field.asset_kinds or ()),
        ))
        return

    if field.fields:
        _walk(field.fields, path + "/", counts, plan)
        return

    plan.sequence.append(
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


def _row_count(counts: Mapping[str, int], path: str) -> int:
    """Rows to expand, clamped to :data:`MAX_ROWS`. A non-number reads as empty rather than
    raising: the store is hand-editable, and refusing to draw the panel would hide the bad key."""
    try:
        value = int(counts.get(path, 0))
    except (TypeError, ValueError):
        return 0
    return max(0, min(value, MAX_ROWS))


def _title_path(items: AuthoredFieldSchema) -> str | None:
    """The first string-ish leaf of a row: a schema declares no "name" member, and the leading
    string is what an author reads as a record's identity (``LootTable.Table``, ``ItemDef.Id``)."""
    for field in items.fields or ():
        if field.type in (TYPE_STRING, TYPE_ENUM) and field.authored_by is None:
            return field.name
    return None


def counts_of(component: AuthoredComponentSchema, payload: Any) -> dict[str, int]:
    """Row counts for every array instance in a payload, keyed as :func:`outline` takes them."""
    counts: dict[str, int] = {}
    _count_into(component.fields, "", payload, counts)
    return counts


def _count_into(
    source: list[AuthoredFieldSchema], prefix: str, node: Any, counts: dict[str, int]
) -> None:
    for field in source:
        value = node.get(field.name) if isinstance(node, Mapping) else None
        _count_field(field, prefix + field.name, value, counts)


def _count_field(
    field: AuthoredFieldSchema, path: str, value: Any, counts: dict[str, int]
) -> None:
    """Mirror of :func:`_walk_field` over data, with the same explicit-path reason."""
    if field.type == TYPE_ARRAY:
        items = field.items
        if items is None or items.authored_by is not None:
            return
        # A member spelled as a non-list is an empty list, not a crash: the panel must still
        # draw so the author can fix it.
        rows = value if isinstance(value, list) else []
        counts[path] = min(len(rows), MAX_ROWS)
        for index, row in enumerate(rows[:MAX_ROWS]):
            _count_field(items, f"{path}/{index}", row, counts)
        return

    if field.authored_by is not None:
        return

    if field.fields:
        _count_into(field.fields, path + "/", value, counts)


# --------------------------------------------------------------------------------------
# Row paths -- the pure algebra the row operators are built from
# --------------------------------------------------------------------------------------


def row_container_of(path: str) -> str:
    """The nearest enclosing row of a path (``Tables/0/Entries/1/Weight`` -> ``Tables/0/Entries/1``),
    or ``""`` outside any list."""
    parts = path.split("/")
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].isdigit():
            return "/".join(parts[: index + 1])
    return ""


def relative_to(path: str, container: str) -> str:
    """``path`` with ``container``'s prefix removed -- the label a row's leaf draws under."""
    if not container:
        return path
    head = container + "/"
    return path[len(head):] if path.startswith(head) else path


def row_index_of(path: str, array_path: str) -> int | None:
    """Which row of ``array_path`` this path belongs to, or None.

    Segment-exact at both ends: without the separator ``Tables`` matches ``TablesEnabled`` and
    renumbers a sibling field; without a whole-segment index ``Tables/10/X`` reads as row 1.
    A nested count key (``Tables/2/Entries#``) belongs to row 2, so the suffix is stripped first.
    """
    head = array_path + "/"
    if not path.startswith(head):
        return None
    segment = path[len(head):].split("/", 1)[0]
    if segment.endswith(COUNT_SUFFIX):
        segment = segment[: -len(COUNT_SUFFIX)]
    return int(segment) if segment.isdigit() else None


def renumber(path: str, array_path: str, mapping: Mapping[int, int | None]) -> str | None:
    """``path`` with its row index under ``array_path`` remapped; None when its row is going away.

    Only the one segment naming the row is rewritten, so every descendant (including a nested
    count key) moves with it. A path outside ``array_path`` comes back unchanged, not None:
    callers rewrite a whole component's keys, and "not mine" must differ from "delete".
    """
    index = row_index_of(path, array_path)
    if index is None:
        return path
    target = mapping.get(index)
    if target is None:
        return None
    segment, _, tail = path[len(array_path) + 1:].partition("/")
    suffix = segment[len(str(index)):]  # "" for a value key, COUNT_SUFFIX for a nested count
    rebuilt = f"{array_path}/{target}{suffix}"
    return f"{rebuilt}/{tail}" if tail else rebuilt


def removal_mapping(count: int, index: int) -> dict[int, int | None]:
    """Remove row ``index``: rows above shift down, and the removed row is absent from the
    mapping, which :func:`renumber` reads as delete."""
    return {i: (i if i < index else i - 1) for i in range(count) if i != index}


def swap_mapping(count: int, a: int, b: int) -> dict[int, int]:
    """Exchange two rows, leaving every other row where it is."""
    mapping: dict[int, int] = {i: i for i in range(count)}
    mapping[a], mapping[b] = b, a
    return mapping


def default_of(field: AuthoredFieldSchema) -> Any:
    """A field's default at its schema type. An enum with no declared default starts on a legal
    member, or the dropdown would open on a value the runtime cannot parse."""
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
    # Unknown leaf type: a number rather than a dropped field, matching the Godot host.
    return float(declared) if _is_number(declared) else 0.0


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --------------------------------------------------------------------------------------
# Payload -- authored values back to the JSON the runtime deserializes
# --------------------------------------------------------------------------------------


def build_payload(
    component: AuthoredComponentSchema,
    values: Mapping[str, Any],
    counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """The component's exported ``Data`` payload from a flat ``{path: value}`` mapping.

    An authorable host reference is written even when the caller baked nothing: an unassigned
    slot then exports the record's own defaults, which the runtime may refuse, and it can only
    refuse an export that is honest rather than one that omits the key and calls it unauthored.
    Non-authorable references are skipped, since absent IS "unauthored" to the reader.

    ``counts=None`` means the caller holds no list data at all (an entity export, which cannot
    author lists yet), so arrays stay absent and that path keeps its historical bytes; passing
    counts, even empty, yields ``[]`` for a list authored with no rows.
    """
    plan = outline(component, counts)
    payload: dict[str, Any] = {}
    for item in plan.sequence:
        if isinstance(item, FlatArray):
            if counts is not None:
                _write_path(payload, item.path, [])
            continue
        _write_path(payload, item.path, _wire_value(item, values.get(item.path, item.default)))

    # Reference leaves are not in `plan.sequence` (the walk stops at a host reference so nothing
    # can edit them as fields); they are written from the schemas the ref carries.
    for host in plan.hosts:
        if not host.is_authorable:
            continue
        if host.leaf_type is not None:
            _write_path(payload, host.path, _wire_value(
                FlatField(path=host.path, type=host.leaf_type, asset_kinds=host.asset_kinds),
                values.get(host.path)))
            continue
        for leaf in host.leaves:
            path = f"{host.path}/{leaf.name}"
            _write_path(payload, path, _wire_value(
                _leaf_field(path, leaf), values.get(path, default_of(leaf))))
    return payload


def _leaf_field(path: str, leaf: AuthoredFieldSchema) -> FlatField:
    """A baked leaf as the flat field ``_wire_value`` expects."""
    return FlatField(
        path=path,
        type=leaf.type,
        unit=leaf.unit,
        doc=leaf.doc,
        minimum=leaf.minimum,
        maximum=leaf.maximum,
        values=leaf.values,
        visible_when=leaf.visible_when,
        default=default_of(leaf),
        has_default=leaf.has_default,
        asset_kinds=leaf.asset_kinds,
    )


def value_at(payload: Any, path: str, fallback: Any = None) -> Any:
    """Read a slash path out of a payload, the inverse of :func:`_write_path`.

    The container decides how a segment is read, not its spelling: a numeric part indexes a list
    but is a member name against an object, so a record with a member called ``0`` still reads.
    """
    value = payload
    for part in path.split("/"):
        if isinstance(value, list):
            if not part.isdigit() or int(part) >= len(value):
                return fallback
            value = value[int(part)]
        elif isinstance(value, Mapping) and part in value:
            value = value[part]
        else:
            return fallback
    return fallback if value is None else value


def _wire_value(field: FlatField, value: Any) -> Any:
    if field.type == TYPE_BOOL:
        return bool(value)
    if field.type == TYPE_INT:
        return int(value)
    if field.type in (TYPE_STRING, TYPE_ENUM):
        text = str(value) if value is not None else ""
        if field.type == TYPE_ENUM:
            # A name outside the schema's list is a runtime parse error with this entity's
            # name nowhere in the message.
            if field.values and text not in field.values:
                return field.values[0]
            return text
        # No declared default means the record had no initializer, so its default is null.
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
    if field.type in (TYPE_OBJECT, TYPE_ARRAY):
        # Null, never the float fall-through: `"NavObstacle": 0` made the payload unreadable as
        # its record, so the runtime dropped the WHOLE component and the volume silently vanished.
        return None
    return float(value)


def _write_path(root: dict[str, Any], path: str, value: Any) -> None:
    """Write a slash path into a nested payload, creating objects and lists as needed.

    Which container a segment creates is decided by the segment AFTER it: ``Tables/0/Table``
    means a list at ``Tables`` and an object at index 0. A hole in a hand-built mapping yields
    an empty row (which the generated reader fills from initializers) rather than a JSON null
    (which it would dereference); nothing compacts holes, since reindexing would hide the bug.
    """
    parts = path.split("/")
    target: Any = root
    for depth, part in enumerate(parts[:-1]):
        target = _child(target, part, wants_list=parts[depth + 1].isdigit())
    _assign(target, parts[-1], value)


def _child(container: Any, part: str, wants_list: bool) -> Any:
    """The child container at ``part``, created or replaced to be the kind the next segment needs."""
    kind: Any = list if wants_list else dict
    if isinstance(container, list):
        index = int(part)
        _grow(container, index, kind)
        if not isinstance(container[index], kind):
            container[index] = kind()
        return container[index]
    nested = container.get(part)
    if not isinstance(nested, kind):
        nested = kind()
        container[part] = nested
    return nested


def _assign(container: Any, part: str, value: Any) -> None:
    if isinstance(container, list):
        index = int(part)
        _grow(container, index, lambda: None)
        container[index] = value
    else:
        container[part] = value


def _grow(target: list[Any], index: int, kind: Any) -> None:
    """Extend a list so ``index`` exists. ``kind`` is a factory: one shared ``{}`` appended twice
    makes two rows the same object, which no single-row test catches."""
    while len(target) <= index:
        target.append(kind())
