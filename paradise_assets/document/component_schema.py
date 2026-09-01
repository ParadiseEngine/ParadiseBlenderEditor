"""The game's ``authoring-schema.json``, read as a vocabulary of EDITABLE fields.

:mod:`.schema` reads the same file for one question -- which fields name a mesh -- and answers it
with a heuristic when the dump is missing. This module is the other reader, and it cannot do
that: an editor needs a field's TYPE to draw a widget, its default to know what an absent value
means, and its allowed values to keep an enum an enum. There is no honest fallback for any of
those, so a component the schema does not describe is simply not editable here, and the panel
says so rather than guessing.

**The schema is the GAME's, and since contract v6 it is the only one there is.** The engine
declares no authored components at all, so there is no engine tier to consult first and no
vendored copy to merge -- the launcher's dump is the whole vocabulary. A game that has never been
built has no dump, which is a normal state for a fresh clone and reads here as "nothing is
editable yet" rather than as an error.

**Two components are deliberately absent from it, and must stay that way.** ``meta`` and
``transform`` belong to the FORMAT (see :mod:`..well_known`): closed schemas, fixed ids, written
by every host, and no game may add a field to either. They are the object's identity and its
placement, edited through Blender's own name field and transform gizmo, and this module refuses
to describe them so no panel can offer a second way to type them in.
"""

from __future__ import annotations

import json
import os

from . import well_known

__all__ = ["ComponentSchema", "FieldSchema", "Vocabulary", "load"]

#: Where a dumped schema is looked for, relative to the project root, in order.
#:
#: Shared with :mod:`.schema` by copy rather than by import, because the two readers are
#: independent and the day one of them needs a different search order it should be able to say so.
_CANDIDATES = ("build/authoring-schema.json", "data/authoring-schema.json")

#: Field types this addon can present an editor for.
#:
#: The two it cannot are ``object`` and ``array``: a nested payload needs the panel to address a
#: value by PATH rather than by name, and a list needs add/remove/reorder before an edit means
#: anything. Both are shown read-only, which is what the panel did for everything until now, so
#: nothing regressed by leaving them.
EDITABLE_TYPES = frozenset({"bool", "int", "float", "string", "enum", "vector3", "color"})


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

    @property
    def editable(self) -> bool:
        """Whether this addon can offer an editor for it.

        A HOST-AUTHORED field is excluded even when its type is editable, and that is the point of
        the flag rather than an oversight: ``[AuthoredByHost]`` means the value is derived from
        the host object -- the mesh it points at, the shape drawn with the host's handles -- so
        typing a path into it would be authoring in the one place the export is going to
        overwrite.
        """
        return self.type in EDITABLE_TYPES and self.authored_by is None

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
        """The component with this id, or ``None`` -- including for the format's own two."""
        if not component_id:
            return None
        return self._components.get(component_id.lower())


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


def _short(type_name: str | None) -> str | None:
    """The last segment of a CLR type name -- what an author recognises."""
    if not isinstance(type_name, str) or not type_name:
        return None
    return type_name.rsplit(".", 1)[-1]
