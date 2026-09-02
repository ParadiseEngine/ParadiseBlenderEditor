"""The game's ``authoring-schema.json``, read as a vocabulary of EDITABLE fields.

:mod:`.schema` reads the same file for one question -- which fields name a mesh -- and answers it
with a heuristic when the dump is missing. This module is the other reader. An editor needs a
field's TYPE to draw a widget, its default to know what an absent value means, and its allowed
values to keep an enum an enum.

**The dump is the GAME's, and since contract v6 the engine declares no ``[Authored]`` types.**
That does not mean engine payloads have vanished: prefabs still carry
``Paradise.Export.Data.RigidbodyComponentData`` and friends, they just never appear in the dump.
:func:`describe` fills that gap in three steps: the dump, then a host-side form for the engine
types authors still type (rigidbody), then a type inferred from the payload itself. A game that
has never been built has no dump; engine forms and inference still work.

**Two components are deliberately absent, and must stay that way.** ``meta`` and ``transform``
belong to the FORMAT (see :mod:`.well_known`): closed schemas, fixed ids, written by every host.
They are the object's identity and its placement, edited through Blender's own name field and
transform gizmo. Host-derived types (mesh, collider shapes, lamp) stay locked -- a form would
fight the bake. Asset references (material slots) are the opposite: the document stores them,
and this panel is how an author picks the file.
"""

from __future__ import annotations

import copy
import json
import os

from . import well_known

__all__ = [
    "ComponentSchema",
    "EDITABLE_TYPES",
    "FieldSchema",
    "PlanItem",
    "ROLE_ARRAY",
    "ROLE_LEAF",
    "ROLE_LOCKED",
    "ROLE_ROW",
    "Vocabulary",
    "addable",
    "default_payload",
    "describe",
    "format_value",
    "infer",
    "is_asset_field",
    "is_asset_ref",
    "is_format_owned",
    "is_host_derived",
    "field_caption",
    "has_slider",
    "id_subtype",
    "is_host_locked",
    "join_path",
    "load",
    "numeric_widget_options",
]

#: Where a dumped schema is looked for, relative to the project root, in order.
#:
#: Shared with :mod:`.schema` by copy rather than by import, because the two readers are
#: independent and the day one of them needs a different search order it should be able to say so.
_CANDIDATES = ("build/authoring-schema.json", "data/authoring-schema.json")

#: Field types this addon can present an editor for. Nested records flatten to slash paths
#: (``Camera/Guide/NearDistance``) and lists expand to indexed rows (``Slots/0``); both are
#: addressable, which is why they are not in this set -- they are containers, not leaves.
EDITABLE_TYPES = frozenset({
    "bool", "int", "float", "string", "enum", "vector2", "vector3", "quaternion", "color",
    "asset",
})

#: Host kinds that mean "Blender already authors this". A form would fight the bake.
#: ``asset`` and ``entity`` are NOT in this set: they are references the document stores, and
#: this addon is the place an author picks them.
_HOST_LOCKED = frozenset({
    "mesh", "shape", "sprite", "light", "camera", "transform",
    "parent", "id", "name", "local-position", "local-rotation", "local-scale",
})

#: How the Components panel treats one schema member. ``leaf`` is an editor, ``array`` a list
#: header with add, ``row`` one element of that list with remove, ``locked`` a host-authored
#: value shown and not offered.
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
        #: ``[AuthoredByHost]``'s kind, when the host supplies this value rather than an author.
        self.authored_by: str | None = raw.get("authoredBy")
        #: The extensions an asset-typed field accepts, when it names a file.
        self.asset_kinds: list[str] = list(raw.get("assetKinds") or [])
        #: An enum's allowed values, in declaration order.
        self.values: list[str] = list(raw.get("values") or [])
        self.fields: list[FieldSchema] = [
            FieldSchema(child) for child in raw.get("fields") or [] if isinstance(child, dict)
        ]
        items = raw.get("items")
        self.items: FieldSchema | None = FieldSchema(items) if isinstance(items, dict) else None
        # ``[AuthorAssetKinds]`` sits on the LIST property in C#, so the dump writes it on the
        # array field. The picker is per row; copy the kinds down so each item is an asset.
        if self.type == "array" and self.items is not None and self.asset_kinds and not self.items.asset_kinds:
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
        """Whether this addon can offer an editor for it.

        A HOST-OBJECT field stays locked: ``[AuthoredByHost(mesh)]`` is derived from the Blender
        object, and typing a path into it would be authoring in the place the bake overwrites.
        An ``asset`` or ``entity`` reference is the opposite -- the document stores it, and this
        panel is how an author picks the file or the object.
        """
        if is_host_locked(self):
            return False
        if self.type == "array":
            # The list is edited by adding and removing rows, not as a single value. Items that
            # the host bakes stay locked, so the Add button is not offered over them either.
            return self.items is not None and not is_host_locked(self.items)
        return self.type in EDITABLE_TYPES or is_asset_field(self)

    def default_value(self):
        """What an absent leaf, or a newly added list row, should hold.

        The dump's ``default`` wins. Falling back to a typed zero rather than ``None`` keeps the
        overlay JSON from recording a JSON null the generated reader would not materialize the
        same way as an omitted key.
        """
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
        """*value* held to the declared range, when it declares one.

        ``[AuthorRange]`` is ADVISORY in the engine -- nothing clamps at load, and the game's own
        validation is what refuses an unplayable value -- so this is a courtesy to the person
        dragging the widget, not an enforcement. It is applied on the way IN so the document never
        records a number the panel would not have let you type.
        """
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
    """Whether this member is picked as a file, not typed as a string.

    The dump says so via ``authoredBy: asset`` / ``assetKinds``. A payload that already holds
    ``{ guid, path }`` is one too -- AuthoredMaterials still publishes items as ``string``, but
    the documents store references, and the picker has to follow the documents. A mesh/shape
    field may also list ``assetKinds``; those stay host-locked, not pickable here.

    An ARRAY is never itself a picker, even when ``[AuthorAssetKinds]`` sat on the list
    property and the dump wrote ``assetKinds`` there. Those kinds apply to each ROW; treating
    the list as one asset is what made Materials a single slot with no add/remove.
    """
    if is_host_locked(field) or field.type == "array":
        return False
    if field.authored_by == "asset" or field.type == "asset" or field.asset_kinds:
        return True
    names = {child.name.lower() for child in field.fields}
    if names >= {"guid", "path"} and len(field.fields) <= 2:
        return True
    return is_asset_ref(value)


#: Schema ``unit`` → Blender ID-property subtype. These are what make the NUMBER FIELD itself
#: show a unit (``m``, ``°``, a 0–1 factor) rather than a bare float. ``kilograms`` has no
#: subtype -- Blender's ID properties cannot say MASS -- so the caption carries ``kg``.
#:
#: Mirrored in ``paradise_blender.contract.authoring``: the two addons do not import each other.
_SUBTYPE_FOR_UNIT = {
    "meters": "DISTANCE",
    "radians": "ANGLE",
    "seconds": "TIME",
    "unit01": "FACTOR",
}

#: Shown in the label only when the widget itself will not. Unknown units print as declared.
_SHORT_UNIT = {
    "kilograms": "kg",
}


def id_subtype(unit: str | None) -> str | None:
    """Blender ID-property subtype for a schema unit, or None when the widget cannot carry it."""
    return _SUBTYPE_FOR_UNIT.get(unit) if unit else None


def field_caption(name: str, unit: str | None) -> str:
    """The label drawn next to a number field.

    Units the widget already displays (metres, radians, seconds, 0–1) stay off the label so the
    row does not read ``Mass (kilograms): 1.0 kg``. Kilograms has no Blender subtype, so it
    becomes ``(kg)``. Anything else the schema named is shown as declared.
    """
    if not unit or unit in _SUBTYPE_FOR_UNIT:
        return name
    return f"{name} ({_SHORT_UNIT.get(unit, unit)})"


def has_slider(minimum, maximum, unit: str | None = None) -> bool:
    """Whether both ends of a range exist, so Blender can draw a capped slider.

    ``[Unit01]`` always qualifies -- the generator writes 0..1, and even an inferred field that
    only carries the unit should still cap rather than free-drag.
    """
    if unit == "unit01":
        return True
    return isinstance(minimum, (int, float)) and isinstance(maximum, (int, float))


def numeric_widget_options(field: FieldSchema) -> dict:
    """ID-property UI metadata for a float/int: range, factor, distance, angle, time.

    Applied on the way IN so a drag cannot leave the declared ``[AuthorRange]``, and so
    ``[Unit01]`` is a 0–1 factor rather than a free float. Advisory in the engine; here it is
    a courtesy to the person dragging the widget.
    """
    if field.type not in ("float", "int"):
        return {}
    options: dict = {}
    if isinstance(field.minimum, (int, float)):
        options["min"] = field.minimum
    if isinstance(field.maximum, (int, float)):
        options["max"] = field.maximum
    subtype = id_subtype(field.unit)
    if subtype:
        options["subtype"] = subtype
    if field.unit == "unit01":
        options.setdefault("min", 0.0)
        options.setdefault("max", 1.0)
        options["subtype"] = "FACTOR"
    return options


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
    # Arrays before the asset shortcut: ``assetKinds`` on the LIST would otherwise make the
    # whole list one picker and hide add/remove. Rows still go through is_asset_field below.
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
        """The schema member at a slash path, including indexed list rows.

        ``MaxSpeed`` is a top-level leaf. ``Camera/Guide/NearDistance`` walks nested records.
        ``Slots/0`` is the items schema of ``Slots`` at row 0. Numeric segments never name a
        field -- they select a row -- so a path the dump did not declare returns None rather
        than the parent.
        """
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
        """Everything the Components panel draws, in declaration order, expanded to *data*.

        Host-authored members stay one locked row -- their children are what the bake fills, and
        offering those as typed fields would be a second, disagreeing copy. Nested records and
        lists the HOST does not author flatten to leaves the overlay can address by path.
        """
        items: list[PlanItem] = []
        _walk(self.fields, "", data if isinstance(data, dict) else {}, items)
        return items


class Vocabulary:
    """Every component the game declares, by id."""

    def __init__(self, components: dict[str, ComponentSchema], source: str | None) -> None:
        self._components = components
        #: Where the dump came from, or ``None`` when there is none.
        self.source = source

    def __bool__(self) -> bool:
        return bool(self._components)

    def __iter__(self):
        """In display-name order, which is the order a picker should offer them in."""
        return iter(sorted(self._components.values(), key=lambda c: c.display_name.lower()))

    def get(self, component_id: str | None) -> ComponentSchema | None:
        """The DUMPed component with this id, or ``None`` -- including for the format's own two.

        Panels and editors should call :func:`describe` instead: that is the one that still
        offers an engine rigidbody after v6 stopped putting it in the dump.
        """
        if not component_id:
            return None
        return self._components.get(component_id.lower())

    def describe(self, component: dict) -> ComponentSchema | None:
        """The schema this payload should be edited through, or ``None`` when it should not."""
        return describe(component, self)


def load(project_root: str) -> Vocabulary:
    """Read the game's dump, or return an empty vocabulary when there is none."""
    for candidate in _CANDIDATES:
        path = os.path.join(project_root, candidate.replace("/", os.sep))
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as handle:
                document = json.load(handle)
        except (OSError, json.JSONDecodeError):
            # An unreadable dump reads as "no vocabulary", the same as an absent one. It is a
            # build product; failing the panel over it would make a half-written file break
            # opening a document that is otherwise fine.
            continue

        components: dict[str, ComponentSchema] = {}
        for raw in document.get("components", []):
            if not isinstance(raw, dict):
                continue
            component = ComponentSchema(raw)
            # The format's own two are refused even if a game's dump happens to describe them:
            # their schemas are closed, and an editor for them would be a second way to author
            # the identity and the placement Blender already owns.
            if not component.id or component.id.lower() in _FORMAT_OWNED:
                continue
            components[component.id.lower()] = component

        return Vocabulary(components, path)

    return Vocabulary({}, None)


_FORMAT_OWNED = frozenset({well_known.META_ID.lower(), well_known.TRANSFORM_ID.lower()})

#: Engine components this host authors from Blender data, not a form. Matching the
#: ``paradise_blender`` export split: material slots, mesh, collider shapes, lamp.
#: A dump that redeclares one (ShiningPie's ``AuthoredMaterials``) still wins -- that is a
#: game type under the engine id, and the dump is what says how to edit it.
_HOST_DERIVED = frozenset({
    "f2c0357e-94dd-4a5a-9803-518066cb54b2",  # renderable
    "e1cd1bc8-86f2-4225-adc9-4a324c70ebf9",  # collider
    "fc886b84-c48c-4415-afd9-b03d6faf5ab7",  # light
})

#: ``Paradise.Export.Data.PhysicsBodyType``, serialized by name.
_PHYSICS_BODY_TYPES = ("None", "Static", "Kinematic", "Dynamic")

#: ``[Guid("b7ab4dd8-c8da-4dc2-9e5e-192fd74deb11")]`` on the engine rigidbody record.
#:
#: The engine stopped declaring it ``[Authored]`` in v6, so it is absent from every game dump,
#: but prefabs still carry it and authors still change Mass / BodyType. Transcribed rather than
#: imported from ``paradise_blender``: the two addons are independent artifacts.
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
    """Types the Components panel's Add button should offer for this object.

    Everything the dump names, plus engine forms the dump no longer lists (rigidbody), minus
    what is already on the object, minus ``meta`` / ``transform``, minus host-derived types
    whose payload the bake overwrites. One of each id -- a dump that redeclares rigidbody wins.
    """
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
    """The schema this payload should be edited through, or ``None`` when it should not.

    Order: the game dump, a host-side engine form, a type inferred from the values. ``meta`` /
    ``transform`` and host-derived engine types return None so the panel can draw them from
    live Blender state (or as a locked dump) instead of offering a second, disagreeing editor.
    """
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
    """A synthetic schema from the payload, for types the dump does not describe.

    Guessing a type is worse than a dump, and better than a panel that can only print the
    values. Enums the payload does not name stay strings; BodyType is recognised by field name
    because every rigidbody in the wild carries it and a dropdown is the honest widget.
    """
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
