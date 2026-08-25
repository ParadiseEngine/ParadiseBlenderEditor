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
* A LIST extends the same grammar with an index segment: ``"Tables/0/Entries/1/Weight"``. The
  schema declares that a member *is* a list and can say nothing about how long it is, so the
  row count is DATA and arrives from the caller -- see :func:`outline` and :func:`counts_of`.
  A numeric segment re-nests into a JSON array rather than an object, which is the only place
  the two halves of this module need to agree about a spelling.
* Fields (or whole components) with ``authoredBy`` are host-object *references*: authored by
  pointing at one of the host's own objects, exported as the numbers baked out of it. This host
  implements TWO kinds -- :data:`HOST_TRANSFORM`, whose world pose is baked into the reference's
  own leaves at export (``authored_components.bake_transform_refs``), and :data:`HOST_SHAPE`,
  whose collider is (``bake_shape_refs``). Both are the same object slot; only what the exporter
  takes off what you point at differs. Every other kind is still reported and skipped, which the
  reader treats as unauthored;
  :func:`flatten` surfaces them so the UI can say so instead of drawing a control that exports
  nothing. An ``authoredBy`` LIST stays a reference regardless: a row editor over a collider's
  shapes would be a second, lying copy of the pointer list the entity already holds.

No ``bpy`` import: this module is pure data and is unit-tested standalone.
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
    "flatten",
    "merge",
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

# AuthoringSchemaDocument.CurrentVersion / MinimumSupportedVersion. Bump only in lockstep with
# the engine.
#
# The minimum EQUALS the current, and that is deliberate rather than an oversight. v3 made `id` a
# GUID; a v1 or v2 document keys its components by a NAME, and there is no way to derive a
# component's GUID from `paradise.rigidbody`. Such a document cannot be upgraded on the way in,
# only regenerated -- so it is refused, which names the problem, instead of being read into
# components whose ids resolve to nothing.
CURRENT_VERSION = 3
MINIMUM_SUPPORTED_VERSION = 3

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

#: ``authoredBy`` kinds. The engine's closed set is shape/mesh/sprite/light/asset/transform
#: (``Paradise.Authoring``'s ``AuthoredBySources``); this host implements exactly one of them.
HOST_TRANSFORM = "transform"

#: A reference to a COLLISION SHAPE -- the engine's ``AuthoredBySources.Shape``. Authored as an
#: object slot exactly as a transform is; what differs is that the exporter bakes the collider
#: drawn on the object rather than where the object stands.
HOST_SHAPE = "shape"

#: Pose leaves a ``transform`` reference can bake into, by NAME. A record declares whichever
#: parts of the pose it means and an exporter fills those, ignoring the rest -- which is what
#: keeps the host general: it never learns what a particular record means by a pose.
TRANSFORM_FIELDS = ("Position", "Rotation", "Yaw", "Scale")

# AuthoredBySources. (v1 spelled this kind "nativeShape" and it was normalized on read; the v3
# floor makes such a document unreadable, so the alias is gone rather than dead.)
SOURCE_SHAPE = "shape"

#: Ceiling on the rows one list may hold.
#:
#: A clamp, not a policy. A row count reaches :func:`outline` from a store an author can hand-edit
#: in Blender's Custom Properties panel and from a file the game writes; a ``draw()`` that loops a
#: billion times hangs Blender with no way back to the button that would fix it. Far above any
#: real authored list, so nothing legitimate ever meets it.
MAX_ROWS = 4096

#: Suffix marking a stored row COUNT rather than a value: ``Tables#`` holds how many rows
#: ``Tables`` has, ``Tables/0/Entries#`` how many the entries of table 0 has.
#:
#: This is storage naming, and it lives here only because the path algebra is forced to recognize
#: it: :func:`renumber` has to carry ``Tables/2/Entries#`` along with ``Tables/2/Entries/0/Weight``
#: when row 2 moves, which it cannot do without knowing the suffix exists. It never reaches a file.
#:
#: ONE character, deliberately -- see ``config_store`` for the budget it is spent against. ``#``
#: is safe because it cannot occur in a C# member name and is not in base64url's alphabet, so no
#: field path and no component token can ever produce one.
COUNT_SUFFIX = "#"


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

    #: The component's stable identity, a GUID in canonical lowercase-hyphenated form. The only
    #: member anything may match on.
    id: str = ""

    #: Fully qualified CLR name, e.g. ``Paradise.Export.Data.RigidbodyComponentData``. The
    #: FALLBACK key, and what makes a GUID id survivable in a text document: it is how a human
    #: reading a schema, a diff, or a broken payload tells which component a bare GUID means.
    #: Copied verbatim onto the exported payload -- never synthesized, because the engine's
    #: type-name fallback is an exact ordinal match.
    type: str = ""

    display_name: str = ""
    gizmo: AuthoredGizmoSchema | None = None
    authored_by: str | None = None
    fields: list[AuthoredFieldSchema] = dataclass_field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AuthoredComponentSchema:
        # Lowercased once, here, mirroring AuthoredModel's `parsed.ToString("D")`: a hand-typed
        # uppercase [Guid] in a game repo would otherwise open a SECOND storage namespace on the
        # same object, and the two would not see each other's values.
        component_id = data.get("id", "").strip().lower()
        component_type = data.get("type", "")
        return cls(
            id=component_id,
            type=component_type,
            # Falls back to the TYPE, not the id -- a bare GUID is not a label anyone can read.
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
    ordered by TYPE -- ``AuthoringSchemaReader.Merge``. Earlier-wins so a host can pass the
    engine's schema first and have it be authoritative.

    Ordered by type rather than by id because an id is a GUID: sorting on it would shuffle the
    list into an order no reader could predict, and every consumer here wants a stable one (the
    panel draws in it, and ``build_component_payloads`` exports in it so two exports of the same
    scene agree)."""
    by_id: dict[str, AuthoredComponentSchema] = {}
    for document in documents:
        for component in document.components:
            if component.id and component.id not in by_id:
                by_id[component.id] = component
    return AuthoringSchemaDocument(
        components=sorted(by_id.values(), key=lambda component: (component.type, component.id))
    )


# ---------------------------------------------------------------------------------------------
# The game's schema, by data directory.
#
# HERE, in the Blender-free layer, rather than beside the panel that draws it — because
# component_ids.engine_type_name has to read it during EXPORT, and contract/ may not import
# anything that imports bpy. paradise_blender.authoring.authored_components re-exports both
# functions, so every existing caller keeps its spelling.
# ---------------------------------------------------------------------------------------------

# One cache entry per data directory: (stamp, document, error). Keyed by directory rather than
# held as a single value because two .blend files in different projects can be open in one
# Blender session.
_cache: dict[str, tuple[tuple[int, int], AuthoringSchemaDocument, str | None]] = {}


def schema_for_data_dir(data_dir: str) -> AuthoringSchemaDocument:
    """The game's schema, re-read when the file changes.

    ONE DOCUMENT, AND IT IS THE GAME'S. This host used to merge a vendored copy of the engine's
    own schema underneath it, because the game's dump described only the game. It no longer has
    to: a launcher built with ``ParadiseAuthoringScanReferences`` merges every assembly it
    references — the engine included — into the document it dumps, so the file already describes
    everything an editor can author. The vendored copy was then not merely redundant but
    ACTIVELY WRONG: merges are first-wins, so a checked-in copy that had drifted from the engine
    the game actually builds against would win against the truth.

    The consequence to know: with nothing vendored there is no floor. A data directory with no
    dumped schema yields an EMPTY document — not the engine's components as before — so the
    Components panel is empty until the game is built once. That is what
    :func:`schema_load_error` is for, and the panel says it loudly.

    A missing or unreadable file still yields an empty document rather than an exception: every
    caller sees "no components" instead of dying mid-draw."""
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
            # Named loudly: the symptom of a silently skipped schema is "my component is
            # missing", which gives an author nothing to go on.
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
    """A field authored by REFERENCING a host object rather than by typing its numbers.

    ``kind`` names what the object IS (see ``HOST_TRANSFORM`` and the engine's
    ``AuthoredBySources``). Whether THIS host can author one depends on the kind:
    :data:`HOST_TRANSFORM` is authorable here -- an object slot, baked at export -- and every
    other kind is still reported so the UI can say what is missing and why, instead of silently
    exporting a component with holes.

    ``leaves`` is the field schemas the reference fills, for an authorable kind: the record
    declares which parts of the pose it means, and that list IS the contract between the picker
    and the exporter.

    The SCHEMAS travel, not their names, and that is load-bearing. They are captured during the
    walk, where the parent field is in hand — the alternative was re-deriving them afterwards by
    matching the ref's path against the component's fields, which worked only for a top-level
    reference: ``path`` is built by the same ``prefix + name`` recursion every composed field
    uses, so a reference nested inside one (``Container/Destination``) matched no top-level name
    and its leaves came back EMPTY. The pose baked correctly and was then dropped from the payload
    entirely, with no warning on either side. A second traversal that has to stay in step with the
    first is the bug; not having one is the fix.
    """

    path: str
    kind: str
    is_list: bool = False
    leaves: tuple[AuthoredFieldSchema, ...] = ()

    @property
    def bakes(self) -> tuple[str, ...]:
        """The leaf NAMES, for a caller that only needs to know which parts of the pose to fill."""
        return tuple(leaf.name for leaf in self.leaves)

    @property
    def is_authorable(self) -> bool:
        """Whether this host can actually author the reference, rather than only report it.

        TWO KINDS now. Both are an object slot and differ only in what the exporter bakes out of
        what you point at: a :data:`HOST_TRANSFORM` takes where the object STANDS, a
        :data:`HOST_SHAPE` takes the collider drawn ON it. The picker is identical, which is the
        point -- a kind is a statement about what the object IS, and a host implements as many of
        them as it can rather than one.
        """
        return (
            self.kind in (HOST_TRANSFORM, HOST_SHAPE)
            and not self.is_list
            and bool(self.leaves)
        )


@dataclass
class FlatArray:
    """One authored LIST, addressed by the instance path its rows hang under.

    ``path`` is an INSTANCE path, not a schema path: the ``Entries`` of table 0 and of table 1 are
    two ``FlatArray`` entries (``Tables/0/Entries``, ``Tables/1/Entries``) because they hold
    different numbers of rows. No schema path can express that, which is the whole reason
    :func:`outline` takes counts instead of deriving them.

    Reported separately from fields and host refs because a list is neither: an empty one has no
    leaves at all, and the UI still has to draw its header and its Add button.
    """

    path: str
    #: The declaring member's name, for a panel header. The last segment of ``path`` for a list
    #: nested inside a row, where the schema's own ``name`` is empty.
    label: str
    count: int
    #: ``items.fields`` -- False for a scalar list (``List<string>``), where a row IS one widget
    #: and there is no container to walk into.
    rows_are_records: bool
    #: First string-ish leaf of a row, RELATIVE to the row (``"Table"``), so a panel can title a
    #: row by its content rather than by its index alone. None when a row has no such leaf.
    row_title_path: str | None = None
    doc: str | None = None


@dataclass
class FlatOutline:
    """Everything :func:`outline` found: the editable leaves, the host references, the lists.

    ``sequence`` is the leaves and lists INTERLEAVED in declaration order, and it is the primary
    result -- ``fields`` and ``arrays`` are filtered views of it, materialized once, so the three
    can never drift out of step. The interleaving is what keeps a written payload's keys in
    schema order: seeding every list before every leaf would hoist each row's nested list above
    its siblings, turning a no-op save into a whole-file diff.
    """

    sequence: list[FlatField | FlatArray] = dataclass_field(default_factory=list)
    hosts: list[HostRef] = dataclass_field(default_factory=list)
    fields: list[FlatField] = dataclass_field(default_factory=list)
    arrays: list[FlatArray] = dataclass_field(default_factory=list)


def outline(
    component: AuthoredComponentSchema, counts: Mapping[str, int] | None = None
) -> FlatOutline:
    """The component's field tree as leaf paths, the host references it wants baked, and the
    lists it declares -- expanded to ``counts`` rows apiece.

    Mirrors ``AuthoredEntityCore.ReadFields``: composed fields recurse with a path prefix and
    ``authoredBy`` fields become host references rather than editable leaves. Beyond it, a list
    expands to one subtree per row, at indexed paths.

    ``counts`` maps an array's INSTANCE path to its row count. Absent (or ``None``) means every
    list is empty, which is what a schema alone can say. Callers with data derive it: from a
    payload with :func:`counts_of`, or from a store with ``config_store.counts_for_store``.

    ``arrays`` comes out parent-before-child and rows in ascending index -- ``Tables``, then
    ``Tables/0/Entries``, then ``Tables/1/Entries``. :func:`build_payload` seeds in that order and
    depends on it: a nested list can only be created once the row holding it exists.
    """
    plan = FlatOutline()
    _walk(component.fields, "", counts or {}, plan)
    plan.fields = [item for item in plan.sequence if isinstance(item, FlatField)]
    plan.arrays = [item for item in plan.sequence if isinstance(item, FlatArray)]
    return plan


def flatten(
    component: AuthoredComponentSchema, counts: Mapping[str, int] | None = None
) -> tuple[list[FlatField], list[HostRef]]:
    """The two-tuple facade over :func:`outline`.

    Kept because most call sites read exactly these two lists, and widening the arity would touch
    every one of them to no purpose. Reach for :func:`outline` when you need ``arrays`` too.
    """
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

    The path is a PARAMETER rather than derived from ``field.name``, and that is the trick the
    whole list expansion rests on: an array element's schema (``field.items``) has an empty name,
    so passing its indexed path lets a row take the exact same branch as a named field. A list of
    records, a list of scalars and a list of lists then need no separate code between them.
    """
    if field.type == TYPE_ARRAY:
        items = field.items
        if items is None or items.authored_by is not None:
            # Still a host reference: the Godot host bakes these from objects the entity points
            # at, and a row editor over them would be a second, lying copy of that pointer list.
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
        # The leaves are what an exporter fills, so they travel with the reference — captured HERE,
        # where the parent field is in hand and the path is already correct at any depth.
        #
        # WHICH leaves depends on the KIND, because the two bakes know different amounts about what
        # they are filling. A pose has a closed vocabulary this contract defines (TRANSFORM_FIELDS),
        # and a record is free to carry others a host would only be inventing meaning for. A SHAPE
        # is the other way round: the exporter bakes a whole ColliderShapeData and writes back
        # whatever names the record declared, so the record itself says which parts of a shape it
        # means -- take them all and let the bake fill the ones it has.
        children = tuple(field.fields or ())
        leaves = (
            tuple(child for child in children if child.name in TRANSFORM_FIELDS)
            if field.authored_by == HOST_TRANSFORM
            else children if field.authored_by == HOST_SHAPE
            else ()
        )
        plan.hosts.append(HostRef(path=path, kind=field.authored_by, leaves=leaves))
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
    """How many rows to expand, clamped to :data:`MAX_ROWS` and floored at zero.

    Clamped rather than trusted -- see :data:`MAX_ROWS`. A count that is not a number at all reads
    as an empty list rather than raising: the store it came from is hand-editable, and refusing to
    draw the whole panel over one bad key would hide the field that says which key.
    """
    try:
        value = int(counts.get(path, 0))
    except (TypeError, ValueError):
        return 0
    return max(0, min(value, MAX_ROWS))


def _title_path(items: AuthoredFieldSchema) -> str | None:
    """The first string-ish leaf of a row, for a panel to title the row by.

    First rather than best: a schema declares no "name" member, and the leading string of a record
    is what an author reads as its identity in practice (``LootTable.Table``, ``ItemDef.Id``).
    """
    for field in items.fields or ():
        if field.type in (TYPE_STRING, TYPE_ENUM) and field.authored_by is None:
            return field.name
    return None


def counts_of(component: AuthoredComponentSchema, payload: Any) -> dict[str, int]:
    """Row counts for every array INSTANCE in a payload, keyed as :func:`outline` takes them.

    This is the only place the addon learns how many rows exist. The schema declares that a member
    IS a list; only the data says how long it is, so every consumer of :func:`outline` ultimately
    traces back to here or to the equivalent scan over stored values.
    """
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
    """Mirror of :func:`_walk_field`, over data instead of over counts.

    Same reason for taking an explicit path: a row is counted by the same branch that counts a
    named member, so a list nested inside a list needs no special case.
    """
    if field.type == TYPE_ARRAY:
        items = field.items
        if items is None or items.authored_by is not None:
            return
        # A member the file spells as something other than a list is an EMPTY list, not a crash:
        # the panel's job is to show the author what is there and let them fix it.
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
    """The nearest enclosing ROW of a path, or ``""`` for one not inside any list.

    ``"Tables/0/Entries/1/Weight"`` -> ``"Tables/0/Entries/1"``; ``"Box/SizeX"`` -> ``""``. Used to
    group leaves under the row that owns them, which is how a panel draws rows without searching
    the whole outline per row.
    """
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
    """Which row of ``array_path`` this path belongs to, or None when it belongs to none.

    Segment-exact at BOTH ends, and both halves earn their keep. The prefix must end at a
    separator or ``Tables`` would match ``TablesEnabled`` and renumber a sibling field along with
    the list; and the index must be a whole segment or ``Tables/10/X`` reads as row 1 of something.

    A count key of a list nested at this row (``Tables/2/Entries#``) belongs to row 2, so the
    suffix is stripped before the digits are read -- that is what carries a nested list's own
    count along when its row moves.
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

    Descendants ride along for free, which is the whole trick: only the ONE segment naming the row
    is rewritten, so ``Tables/2/Entries/1/Weight``, ``Tables/2/Entries#`` and ``Tables/2/Table``
    all move together, in one pass, with no knowledge of what is beneath them.

    A path outside ``array_path`` comes back unchanged rather than as None -- callers rewrite a
    whole component's keys through this, and "not mine" must be distinguishable from "delete".
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
    """Remove row ``index`` of ``count``: everything above shifts down one.

    The removed row is simply ABSENT from the mapping, which :func:`renumber` reads as "delete" --
    so one mapping expresses both the shift and the removal, and no caller has to special-case
    the row that is going away.
    """
    return {i: (i if i < index else i - 1) for i in range(count) if i != index}


def swap_mapping(count: int, a: int, b: int) -> dict[int, int]:
    """Exchange two rows, leaving every other row where it is."""
    mapping: dict[int, int] = {i: i for i in range(count)}
    mapping[a], mapping[b] = b, a
    return mapping


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
    component: AuthoredComponentSchema,
    values: Mapping[str, Any],
    counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """The component's exported ``Data`` payload, from a flat ``{path: value}`` mapping.

    Every plain field is written at its schema type, defaults filling anything ``values`` does
    not carry -- see the module docstring for the wire rules.

    An AUTHORABLE host reference (:data:`HOST_TRANSFORM`) contributes its declared leaves, so a
    baked pose lands under the reference's own object exactly as a composed field would: the
    runtime deserializes one record either way and cannot tell that half of it came from an
    object slot. Its leaves take the record's own defaults when the caller baked nothing, which
    is what an unassigned object slot means -- and leaves the RUNTIME free to refuse that, which
    it can only do if the export is honest rather than inventing a pose.

    Every other host reference is skipped: an absent key is "unauthored" to the reader, which is
    the truthful description of a bake this host does not perform.

    ``counts`` is the exact inverse of :func:`counts_of`, and the ``None`` case is meaningful
    rather than merely a default: it says the caller holds no list data AT ALL -- an entity
    export, which cannot author lists yet -- so arrays stay absent from the payload and that path
    keeps producing the bytes it always has. A caller that passes counts (even empty ones) is
    saying the opposite, and gets ``[]`` for a list it authored with no rows.
    """
    plan = outline(component, counts)
    payload: dict[str, Any] = {}
    for item in plan.sequence:
        if isinstance(item, FlatArray):
            # A list authored with no rows has to reach the file as [] rather than vanish from
            # it: the member IS authored, and it is authored empty. Written INTERLEAVED with the
            # leaves rather than in a pass of its own, so each key lands in schema order and a
            # save with no edits does not reshuffle the file.
            if counts is not None:
                _write_path(payload, item.path, [])
            continue
        _write_path(payload, item.path, _wire_value(item, values.get(item.path, item.default)))

    # Authorable references, after the plain fields. Their leaves are not in `plan.sequence` --
    # `_walk_field` stops at a host reference by design, so nothing can edit them as fields -- so
    # they are written from the schemas the ref CARRIES, which are correct at any nesting depth.
    for host in plan.hosts:
        if not host.is_authorable:
            continue
        for leaf in host.leaves:
            path = f"{host.path}/{leaf.name}"
            _write_path(payload, path, _wire_value(
                _leaf_field(path, leaf), values.get(path, default_of(leaf))))
    return payload


def _leaf_field(path: str, leaf: AuthoredFieldSchema) -> FlatField:
    """A baked leaf as the flat field ``_wire_value`` expects. Built here rather than during the
    walk because these deliberately never enter it — see :func:`build_payload`."""
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
    """Read a slash path out of a payload, following LIST indices as well as object members.

    The exact inverse of :func:`_write_path`, and the CONTAINER decides how a segment is read
    rather than the segment's spelling: a numeric part indexes a list but is a plain member name
    against an object, so a record with a member literally called ``0`` still reads correctly.

    Returns ``fallback`` for anything absent, so a member the file omits falls through to the
    field's schema default rather than storing a hole.
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
    """Write a slash path into a nested payload, creating objects AND lists as needed -- the
    inverse of the flattening that produced the path.

    The rule that makes one grammar serve both containers: **which container a segment creates is
    decided by the segment AFTER it**, not by the segment itself. ``Tables/0/Table`` means a list
    at ``Tables`` and an object at its index 0, and the only place that is legible is the next
    segment's spelling.

    Indices are dense and ascending by construction -- :func:`outline` emits ``0..count-1`` in
    order -- so the growth below never runs in normal use. It exists so a hand-built mapping with
    a hole yields an EMPTY ROW, which the engine's generated reader fills from the record's own
    initializers, rather than a JSON ``null`` that same reader would dereference. Nothing
    compacts a hole away: silently reindexing would hide the bug that produced it.
    """
    parts = path.split("/")
    target: Any = root
    for depth, part in enumerate(parts[:-1]):
        target = _child(target, part, wants_list=parts[depth + 1].isdigit())
    _assign(target, parts[-1], value)


def _child(container: Any, part: str, wants_list: bool) -> Any:
    """The child container at ``part``, created (or replaced, if it is the wrong kind) to hold
    what the next segment needs."""
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
    """Extend a list so ``index`` exists.

    ``kind`` is a FACTORY, not a value. One shared ``{}`` appended twice would make two rows the
    SAME object, so an edit to either would appear in both -- a bug that survives every test
    written against a single row, and shows up only as two rows that will not stop agreeing.
    """
    while len(target) <= index:
        target.append(kind())
