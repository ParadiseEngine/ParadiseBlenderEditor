"""The game's ``authoring-schema.json``, read as a vocabulary of editable fields.

Since contract v6 the engine declares no ``[Authored]`` types, but prefabs still carry engine
payloads (rigidbody), so :func:`describe` falls back from the dump to a host-side engine form to
a type inferred from the payload. ``meta`` and ``transform`` are refused outright: Blender's name
field and gizmo are their editor, and a second way to type an identity is a second thing that
can disagree. Host-derived types (mesh, shape, light) stay locked because a form would fight the
bake; asset references are the opposite, the document stores them and this panel picks them.
"""

from __future__ import annotations

import copy
import json
import os

from . import well_known
from .project import SCHEMA_CANDIDATES

__all__ = [
    "EDITABLE_TYPES",
    "ROLE_ARRAY",
    "ROLE_LEAF",
    "ROLE_LOCKED",
    "ROLE_ROW",
    "ComponentSchema",
    "FieldSchema",
    "PlanItem",
    "Vocabulary",
    "addable",
    "default_payload",
    "describe",
    "field_caption",
    "format_value",
    "infer",
    "is_asset_field",
    "is_asset_ref",
    "is_format_owned",
    "is_host_derived",
    "is_host_locked",
    "join_path",
    "load",
]


EDITABLE_TYPES = frozenset({
    "bool", "int", "float", "string", "enum", "vector2", "vector3", "quaternion", "color",
    "asset",
})

#: ``asset`` and ``entity`` are NOT here: the document stores those and this addon picks them.
_HOST_LOCKED = frozenset({
    "mesh", "shape", "sprite", "light", "camera", "transform",
    "parent", "id", "name", "local-position", "local-rotation", "local-scale",
})

ROLE_LEAF = "leaf"
ROLE_ARRAY = "array"
ROLE_ROW = "row"
ROLE_LOCKED = "locked"


class FieldSchema:
    """One member of one component, as the dump describes it."""

    def __init__(self, raw: dict) -> None:
        self.name: str = raw.get("name", "")
        self.type: str = raw.get("type", "")
        self.doc: str | None = raw.get("doc")
        self.default = raw.get("default")
        self.minimum = raw.get("minimum")
        self.maximum = raw.get("maximum")
        self.unit: str | None = raw.get("unit")
        self.authored_by: str | None = raw.get("authoredBy")
        self.asset_kinds: list[str] = list(raw.get("assetKinds") or [])
        self.values: list[str] = list(raw.get("values") or [])
        self.fields: list[FieldSchema] = [
            FieldSchema(child) for child in raw.get("fields") or [] if isinstance(child, dict)
        ]
        items = raw.get("items")
        self.items: FieldSchema | None = FieldSchema(items) if isinstance(items, dict) else None
        # The dump writes ``assetKinds`` on the list property; the picker is per row.
        if (
            self.type == "array"
            and self.items is not None
            and self.asset_kinds
            and not self.items.asset_kinds
        ):
            self.items.asset_kinds = list(self.asset_kinds)
        visible = raw.get("visibleWhen")
        self.visible_when_field: str | None = None
        self.visible_when_equals = None
        if isinstance(visible, dict):
            field_name = visible.get("field")
            self.visible_when_field = field_name if isinstance(field_name, str) else None
            self.visible_when_equals = visible.get("equals")

    @property
    def editable(self) -> bool:
        """Whether this addon can offer an editor. A host-object field stays locked: typing into
        it would be authoring in the place the bake overwrites."""
        if is_host_locked(self):
            return False
        if self.type == "array":
            # Host-baked items stay locked, so no Add button over them either.
            return self.items is not None and not is_host_locked(self.items)
        return self.type in EDITABLE_TYPES or is_asset_field(self)

    def default_value(self):
        """The dump's default, else a typed zero: a JSON null in the overlay would not
        materialize the way an omitted key does in the generated reader."""
        if self.default is not None:
            return self.default
        if is_asset_field(self):
            return {}
        if self.type == "bool":
            return False
        if self.type == "int":
            return 0
        if self.type == "float":
            return 0.0
        if self.type == "vector2":
            return [0.0, 0.0]
        if self.type == "vector3":
            return [0.0, 0.0, 0.0]
        if self.type == "quaternion":
            return [0.0, 0.0, 0.0, 1.0]
        if self.type == "color":
            return [1.0, 1.0, 1.0, 1.0]
        if self.type == "object":
            return {}
        if self.type == "array":
            return []
        return ""

    def clamp(self, value):
        """*value* clamped to a declared range. Advisory in the engine (nothing clamps at load);
        applied on the way in so the document never records a number the panel refuses."""
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return value
        if isinstance(self.minimum, (int, float)):
            value = max(value, self.minimum)
        if isinstance(self.maximum, (int, float)):
            value = min(value, self.maximum)
        return value


class PlanItem:
    """One row the Components panel draws, addressed by a slash path into the payload."""

    def __init__(
        self, path: str, field: FieldSchema, role: str, index: int | None = None
    ) -> None:
        self.path = path
        self.field = field
        self.role = role
        self.index = index


def join_path(prefix: str, name: str) -> str:
    """A slash path. The empty prefix is the component root, not a leading slash."""
    return f"{prefix}/{name}" if prefix else name


def is_host_locked(field: FieldSchema) -> bool:
    """Whether *field* is derived from a Blender object rather than typed in this panel."""
    return field.authored_by in _HOST_LOCKED


def is_asset_ref(value) -> bool:
    """Whether *value* is an authored ``{ guid, path }`` (or the empty ``{}`` null)."""
    if not isinstance(value, dict):
        return False
    if not value:
        return True
    return isinstance(value.get("guid"), str) and isinstance(value.get("path"), str)


def is_asset_field(field: FieldSchema, value=None) -> bool:
    """Whether this member is picked as a file. A payload already holding ``{ guid, path }``
    counts even when the dump says ``string`` (AuthoredMaterials): the picker follows the
    documents. An array is never itself a picker, or Materials becomes one slot with no add/remove."""
    if is_host_locked(field) or field.type == "array":
        return False
    if field.authored_by == "asset" or field.type == "asset" or field.asset_kinds:
        return True
    names = {child.name.lower() for child in field.fields}
    if names >= {"guid", "path"} and len(field.fields) <= 2:
        return True
    return is_asset_ref(value)


#: Units a Blender widget displays itself (distance, angle, time, factor); the caption carries
#: the rest, kilograms included since there is no MASS subtype.
_WIDGET_UNITS = frozenset({"meters", "radians", "seconds", "unit01"})

_SHORT_UNIT = {
    "kilograms": "kg",
}


def field_caption(name: str, unit: str | None) -> str:
    """The label for a number field: units the widget already displays stay off it."""
    if not unit or unit in _WIDGET_UNITS:
        return name
    return f"{name} ({_SHORT_UNIT.get(unit, unit)})"


def _is_visible(field: FieldSchema, siblings) -> bool:
    """``visibleWhen``: hide the field unless a sibling currently holds the declared value."""
    if not field.visible_when_field:
        return True
    if not isinstance(siblings, dict):
        return True
    return siblings.get(field.visible_when_field) == field.visible_when_equals


def _walk(
    fields: list[FieldSchema], prefix: str, node: dict, items: list[PlanItem]
) -> None:
    for field in fields:
        path = join_path(prefix, field.name)
        value = node.get(field.name) if isinstance(node, dict) else None
        _walk_field(field, path, value, items, node if isinstance(node, dict) else {})


def _walk_field(
    field: FieldSchema, path: str, value, items: list[PlanItem], siblings: dict
) -> None:
    if not _is_visible(field, siblings):
        return
    if is_host_locked(field):
        items.append(PlanItem(path, field, ROLE_LOCKED))
        return
    # Arrays before the asset check, or ``assetKinds`` on the list makes it one picker.
    if field.type == "array":
        if field.items is None or is_host_locked(field.items):
            items.append(PlanItem(path, field, ROLE_LOCKED))
            return
        items.append(PlanItem(path, field, ROLE_ARRAY))
        rows = value if isinstance(value, list) else []
        for index, row in enumerate(rows):
            row_path = join_path(path, str(index))
            items.append(PlanItem(row_path, field.items, ROLE_ROW, index=index))
            if is_asset_field(field.items, row):
                continue
            if field.items.fields:
                _walk(
                    field.items.fields,
                    row_path,
                    row if isinstance(row, dict) else {},
                    items,
                )
        return

    if is_asset_field(field, value):
        items.append(PlanItem(path, field, ROLE_LEAF if field.editable else ROLE_LOCKED))
        return

    if field.fields:
        _walk(field.fields, path, value if isinstance(value, dict) else {}, items)
        return

    items.append(PlanItem(path, field, ROLE_LEAF if field.editable else ROLE_LOCKED))


class ComponentSchema:
    """One authored component: its identity, its display name, and its fields."""

    def __init__(self, raw: dict) -> None:
        self.id: str = str(raw.get("id", ""))
        self.type: str | None = raw.get("type")
        self.display_name: str = raw.get("displayName") or _short(self.type) or self.id
        self.fields: list[FieldSchema] = [
            FieldSchema(field) for field in raw.get("fields") or [] if isinstance(field, dict)
        ]

    def field(self, name: str) -> FieldSchema | None:
        for field in self.fields:
            if field.name == name:
                return field
        return None

    def resolve(self, path: str) -> FieldSchema | None:
        """The schema member at a slash path; a numeric segment selects a row, never a field."""
        node_fields = self.fields
        current: FieldSchema | None = None
        for part in path.split("/"):
            if part.isdigit():
                if current is None or current.items is None:
                    return None
                current = current.items
                node_fields = current.fields
                continue
            current = next((field for field in node_fields if field.name == part), None)
            if current is None:
                return None
            node_fields = current.fields
        return current

    def plan(self, data: dict | None) -> list[PlanItem]:
        """Everything the panel draws, in declaration order, expanded to *data*. A host-authored
        member stays one locked row: its children are what the bake fills."""
        items: list[PlanItem] = []
        _walk(self.fields, "", data if isinstance(data, dict) else {}, items)
        return items


class Vocabulary:
    """Every component the game declares, by id."""

    def __init__(self, components: dict[str, ComponentSchema], source: str | None) -> None:
        self._components = components
        self.source = source

    def __bool__(self) -> bool:
        return bool(self._components)

    def __iter__(self):
        """In display-name order, which is the order a picker should offer them in."""
        return iter(sorted(self._components.values(), key=lambda c: c.display_name.lower()))

    def get(self, component_id: str | None) -> ComponentSchema | None:
        """The dumped component with this id, or ``None``. Editors call :func:`describe`."""
        if not component_id:
            return None
        return self._components.get(component_id.lower())

    def describe(self, component: dict) -> ComponentSchema | None:
        """The schema this payload should be edited through, or ``None`` when it should not."""
        return describe(component, self)


#: project root -> (dump path, mtime_ns, size, vocabulary). The panel asks on every redraw, and
#: re-parsing a schema per frame was the redraw's cost; a rebuild changes the stamp.
_CACHE: dict[str, tuple[str, int, int, Vocabulary]] = {}


def load(project_root: str) -> Vocabulary:
    """Read the game's dump, or return an empty vocabulary when there is none. Cached on the
    dump's ``(mtime, size)``, so a rebuild still shows up without reopening."""
    for candidate in SCHEMA_CANDIDATES:
        path = os.path.join(project_root, candidate.replace("/", os.sep))
        try:
            stat = os.stat(path)
        except OSError:
            continue
        cached = _CACHE.get(project_root)
        if cached is not None and cached[:3] == (path, stat.st_mtime_ns, stat.st_size):
            return cached[3]
        try:
            with open(path, "rb") as handle:
                document = json.load(handle)
        except (OSError, json.JSONDecodeError):
            # A half-written build product must not break opening a document.
            continue

        components: dict[str, ComponentSchema] = {}
        for raw in document.get("components", []):
            if not isinstance(raw, dict):
                continue
            component = ComponentSchema(raw)
            # meta/transform are refused even when a dump describes them (module docstring).
            if not component.id or component.id.lower() in _FORMAT_OWNED:
                continue
            components[component.id.lower()] = component

        vocabulary = Vocabulary(components, path)
        _CACHE[project_root] = (path, stat.st_mtime_ns, stat.st_size, vocabulary)
        return vocabulary

    _CACHE.pop(project_root, None)
    return Vocabulary({}, None)


_FORMAT_OWNED = frozenset({well_known.META_ID.lower(), well_known.TRANSFORM_ID.lower()})

#: Engine components baked from Blender data. A dump that redeclares one (ShiningPie's
#: ``AuthoredMaterials``) still wins: that is a game type under the engine id.
_HOST_DERIVED = frozenset({
    "f2c0357e-94dd-4a5a-9803-518066cb54b2",  # renderable
    "e1cd1bc8-86f2-4225-adc9-4a324c70ebf9",  # collider
    "fc886b84-c48c-4415-afd9-b03d6faf5ab7",  # light
})

#: ``Paradise.Export.Data.PhysicsBodyType``, serialized by name.
_PHYSICS_BODY_TYPES = ("None", "Static", "Kinematic", "Dynamic")

#: The engine rigidbody's id. Absent from every v6 dump, but prefabs still carry it and
#: authors still change Mass / BodyType.
_RIGIDBODY_ID = "b7ab4dd8-c8da-4dc2-9e5e-192fd74deb11"

_ENGINE_FORMS: dict[str, ComponentSchema] = {
    _RIGIDBODY_ID: ComponentSchema({
        "id": _RIGIDBODY_ID,
        "type": "Paradise.Export.Data.RigidbodyComponentData",
        "displayName": "Rigidbody",
        "fields": [
            {"name": "BodyType", "type": "enum", "values": list(_PHYSICS_BODY_TYPES),
             "default": "Dynamic"},
            {"name": "Mass", "type": "float", "minimum": 0.001, "maximum": 10000, "default": 1.0,
             "unit": "kilograms",
             "visibleWhen": {"field": "BodyType", "equals": "Dynamic"}},
            {"name": "LinearDamping", "type": "float", "minimum": 0.0, "maximum": 100,
             "default": 0.2},
            {"name": "Restitution", "type": "float", "minimum": 0.0, "maximum": 1.0,
             "default": 0.2, "unit": "unit01"},
            {"name": "Friction", "type": "float", "minimum": 0.0, "maximum": 1.0,
             "default": 0.5, "unit": "unit01"},
            {"name": "Layer", "type": "int", "default": 0},
            {"name": "LayerName", "type": "string", "default": ""},
        ],
    }),
}


def is_format_owned(component_id: str | None) -> bool:
    """Whether this is ``meta`` or ``transform`` -- Blender's name field and gizmo own them."""
    return bool(component_id) and component_id.lower() in _FORMAT_OWNED


def is_host_derived(component_id: str | None) -> bool:
    """Whether this type is baked from the Blender object (mesh, collider, light)."""
    return bool(component_id) and component_id.lower() in _HOST_DERIVED


def addable(vocabulary: Vocabulary, present_ids) -> list[ComponentSchema]:
    """Types the Add button offers: dump plus engine forms, minus present, format-owned and
    host-derived ids. A dump that redeclares rigidbody wins."""
    present = {str(item).lower() for item in present_ids}
    by_id: dict[str, ComponentSchema] = {}
    for schema in vocabulary:
        by_id[schema.id.lower()] = schema
    for schema in _ENGINE_FORMS.values():
        by_id.setdefault(schema.id.lower(), schema)
    return sorted(
        (
            schema for schema in by_id.values()
            if schema.id
            and schema.id.lower() not in present
            and not is_format_owned(schema.id)
            and not is_host_derived(schema.id)
        ),
        key=lambda schema: schema.display_name.lower(),
    )


def default_payload(schema: ComponentSchema) -> dict:
    """A new component's starting data: each editable field at its schema default."""
    return {
        field.name: copy.deepcopy(field.default_value())
        for field in schema.fields
        if field.editable
    }


def describe(component: dict, vocabulary: Vocabulary | None = None) -> ComponentSchema | None:
    """The schema to edit this payload through (dump, engine form, then inference), or ``None``
    for meta/transform and host-derived types, which must not get a second editor."""
    component_id = str(component.get("id", "")).lower()
    if not component_id or component_id in _FORMAT_OWNED:
        return None
    if vocabulary is not None:
        dumped = vocabulary.get(component_id)
        if dumped is not None:
            return dumped
    if component_id in _HOST_DERIVED:
        return None
    canned = _ENGINE_FORMS.get(component_id)
    if canned is not None:
        return canned
    return infer(component)


def infer(component: dict) -> ComponentSchema | None:
    """A schema inferred from the payload: worse than a dump, better than a panel that can
    only print values."""
    component_id = str(component.get("id", ""))
    if not component_id or component_id.lower() in _FORMAT_OWNED:
        return None
    data = component.get("data")
    fields = [
        _infer_field(name, value)
        for name, value in (data.items() if isinstance(data, dict) else ())
    ]
    return ComponentSchema({
        "id": component_id,
        "type": component.get("type"),
        "displayName": _short(component.get("type")) or component_id,
        "fields": fields,
    })


def format_value(value) -> str:
    """A value as one short line. A panel row is not a document viewer."""
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
            return "(" + ", ".join(f"{float(v):g}" for v in value) + ")"
        return f"[{len(value)} item(s)]"
    if isinstance(value, dict):
        # An asset reference is the dict an author actually recognises, so it reads as its path.
        path = value.get("path")
        return str(path) if isinstance(path, str) else "{…}"
    if value is None:
        return "—"
    text = str(value)
    return text if len(text) <= 28 else text[:27] + "…"


def _infer_field(name: str, value) -> dict:
    if isinstance(value, bool):
        return {"name": name, "type": "bool", "default": value}
    if isinstance(value, int):
        return {"name": name, "type": "int", "default": value}
    if isinstance(value, float):
        return {"name": name, "type": "float", "default": value}
    if isinstance(value, str):
        if name == "BodyType":
            return {
                "name": name, "type": "enum", "values": list(_PHYSICS_BODY_TYPES),
                "default": value if value in _PHYSICS_BODY_TYPES else "Dynamic",
            }
        return {"name": name, "type": "string", "default": value}
    if isinstance(value, list):
        if value and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
            if len(value) == 2:
                return {"name": name, "type": "vector2", "default": list(value)}
            if len(value) == 3:
                return {"name": name, "type": "vector3", "default": list(value)}
            if len(value) == 4:
                return {"name": name, "type": "quaternion", "default": list(value)}
        return {"name": name, "type": "array", "items": _infer_items(value)}
    if isinstance(value, dict):
        return {
            "name": name,
            "type": "object",
            "fields": [_infer_field(key, child) for key, child in value.items()],
        }
    return {"name": name, "type": "string", "default": "" if value is None else str(value)}


def _infer_items(rows: list) -> dict:
    """The items schema of a list, taken from the first row when there is one."""
    if not rows:
        return {"type": "string"}
    inferred = _infer_field("item", rows[0])
    inferred.pop("name", None)
    return inferred


def _short(type_name: str | None) -> str | None:
    """The last segment of a CLR type name -- what an author recognises."""
    if not isinstance(type_name, str) or not type_name:
        return None
    return type_name.rsplit(".", 1)[-1]
