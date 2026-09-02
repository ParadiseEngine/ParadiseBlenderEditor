"""The ``*.prefab`` authoring document, mirroring C# ``PrefabDocument.cs`` and its serializer.

Not the export contract: this keeps what the bake destroys (identity, local transforms, parents,
unresolved references). An object has no privileged members; identity and placement are
components (:mod:`well_known`), which is why one override mechanism covers everything. Reading
is strict like the C# reader; writing goes through :mod:`canonical_toml`, and ``prefab-check``
compares bytes.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field

from . import asset_reference, canonical_toml, well_known
from . import guid as document_guid
from .asset_reference import AssetReference

__all__ = [
    "SUPPORTED_SCHEMA_VERSION",
    "PrefabComponent",
    "PrefabDocument",
    "PrefabDocumentError",
    "PrefabObject",
    "dumps",
    "loads",
]

#: The only ``schema_version`` this addon reads or writes.
SUPPORTED_SCHEMA_VERSION = 1

_DOCUMENT_KEYS = frozenset({"schema_version", "objects"})
_OBJECT_KEYS = frozenset({"prefab", "components"})

#: The three keys a component payload may not use, because they are the entry's own structure.
ID_KEY = "id"
TYPE_KEY = "type"
REMOVED_KEY = "removed"
RESERVED_KEYS = frozenset({ID_KEY, TYPE_KEY, REMOVED_KEY})


class PrefabDocumentError(Exception):
    """A prefab document could not be read, parsed, or validated."""

    def __init__(self, source: str, problem: str) -> None:
        super().__init__(f"Prefab document '{source}' {problem}.")
        self.source = source


@dataclass
class PrefabComponent:
    """One component entry. A payload may not use a reserved key: flattened onto the wire it
    collides with the entry's structure and ``dict.update`` would swallow it silently."""

    id: str
    type: str | None = None
    data: dict = field(default_factory=dict)
    #: On an instance: drop the prefab's component of this id rather than overriding it.
    removed: bool = False

    def __post_init__(self) -> None:
        for key in self.data:
            if key in RESERVED_KEYS:
                raise ValueError(
                    f"a component payload may not use the reserved key '{key}'; "
                    "on the wire it would collide with the component's own structure"
                )


@dataclass
class PrefabObject:
    """One authored object: a prefab reference, if any, and its components."""

    prefab: AssetReference | None = None
    components: list[PrefabComponent] = field(default_factory=list)

    def component(self, component_id: str) -> PrefabComponent | None:
        for candidate in self.components:
            if candidate.id == component_id:
                return candidate
        return None

    @property
    def meta(self) -> PrefabComponent | None:
        return self.component(well_known.META_ID)

    @property
    def guid(self) -> str | None:
        return self._meta_field(well_known.GUID)

    @property
    def name(self) -> str | None:
        return self._meta_field(well_known.NAME)

    @property
    def parent(self) -> str | None:
        return self._meta_field(well_known.PARENT)

    @property
    def target(self) -> str | None:
        return self._meta_field(well_known.TARGET)

    @property
    def dropped(self) -> bool:
        return self.meta is not None and self.meta.data.get(well_known.DROPPED) is True

    def _meta_field(self, key: str) -> str | None:
        """A meta string as C# reads it: ``Name = ""`` IS a name (an instance may override one
        away), and an identity comes back in its canonical spelling whatever the file said."""
        meta = self.meta
        if meta is None:
            return None
        value = meta.data.get(key)
        if not isinstance(value, str):
            return None
        if key == well_known.NAME:
            return value
        return document_guid.canonical(value) if document_guid.parse(value) is not None else value

    @staticmethod
    def with_meta(guid: str, name: str | None = None, parent: str | None = None) -> PrefabObject:
        """An object carrying just a meta component -- the shape every caller needs."""
        data: dict = {well_known.GUID: document_guid.canonical(guid)}
        if name is not None:
            data[well_known.NAME] = name
        if parent is not None:
            data[well_known.PARENT] = document_guid.canonical(parent)
        return PrefabObject(components=[PrefabComponent(well_known.META_ID, well_known.META_TYPE, data)])


@dataclass
class PrefabDocument:
    """The document's objects, in document order. Order is load-bearing."""

    objects: list[PrefabObject] = field(default_factory=list)

    def by_guid(self) -> dict[str, PrefabObject]:
        return {o.guid: o for o in self.objects if o.guid is not None}

    def single_root(self) -> PrefabObject | None:
        """The single object with no parent. ``None`` if there is not exactly one."""
        root = None
        for candidate in self.objects:
            if candidate.parent is not None:
                continue
            if root is not None:
                return None
            root = candidate
        return root

    def root(self) -> PrefabObject:
        """The root, for a document already known to be valid."""
        root = self.single_root()
        if root is None:
            raise ValueError("this document has no single root")
        return root

    @property
    def root_guid(self) -> str:
        return self.root().guid  # type: ignore[return-value]

    def validate(self, source: str) -> None:
        """Exactly one root: an instance places exactly one thing, which is what lets any
        document be instantiated into any other."""
        if not self.objects:
            raise PrefabDocumentError(source, "has no objects")

        roots = [o for o in self.objects if o.parent is None]
        if len(roots) == 1:
            return

        if not roots:
            raise PrefabDocumentError(source, "has no root object (every object declares a parent)")

        names = ", ".join(r.name or r.guid or "?" for r in roots)
        raise PrefabDocumentError(
            source,
            f"has {len(roots)} root objects ({names}); a document has exactly one, because an "
            "instance places exactly one thing -- parent the others beneath it",
        )


def loads(text: str, source: str = "<document>") -> PrefabDocument:
    """Parse and validate a document."""

    def fail(problem: str) -> PrefabDocumentError:
        return PrefabDocumentError(source, problem)

    try:
        root = canonical_toml.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise fail(f"is not valid TOML ({error})") from error
    except ValueError as error:   # a table nested inside an inline element
        raise fail(str(error)) from error

    _reject_unknown(root, _DOCUMENT_KEYS, "at the document root", fail)

    version = root.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise fail(f"must declare 'schema_version = {SUPPORTED_SCHEMA_VERSION}'")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise fail(
            f"declares schema_version = {version}, which this build cannot read "
            f"(supported: {SUPPORTED_SCHEMA_VERSION})"
        )

    document = PrefabDocument()
    parents: dict[str, str | None] = {}
    for index, table in enumerate(root.get("objects", [])):
        obj = _read_object(table, index, fail)

        # A Target carrier addresses a prefab-local object and has no identity of its own -- the
        # resolved child's guid is always minted -- so it is exempt from the uniqueness map.
        if obj.target is not None:
            document.objects.append(obj)
            continue

        if obj.guid is None:
            raise fail(
                f"has an object at index {index} with no '{well_known.META_TYPE}' "
                f"component carrying a '{well_known.GUID}'"
            )
        if obj.guid in parents:
            raise fail(f"declares guid '{obj.guid}' twice -- identities must be unique per document")

        parents[obj.guid] = obj.parent
        document.objects.append(obj)

    _validate_parents(parents, fail)

    document.validate(source)
    return document


def dumps(document: PrefabDocument) -> str:
    """Render a document as canonical TOML. Form is the model's (:mod:`canonical_toml`): a value
    that entered the model from JSON -- the edit overlay -- must have been restored at the door
    (:func:`canonical_toml.restore_inline_tables`), since here a plain ``dict`` in a list is an
    array of tables read from ``[[header]]`` blocks and is written back as such."""
    root: dict = {"schema_version": SUPPORTED_SCHEMA_VERSION}
    if document.objects:
        root["objects"] = [_object_table(o) for o in document.objects]
    return canonical_toml.dumps(root)


def _read_object(table: dict, index: int, fail) -> PrefabObject:
    context = f"on objects[{index}]"
    _reject_unknown(table, _OBJECT_KEYS, context, fail)

    obj = PrefabObject()
    if "prefab" in table:
        obj.prefab = asset_reference.read(table["prefab"], context, fail)
        if obj.prefab is None:
            raise fail(f"has an empty 'prefab' reference {context}")

    seen: set[str] = set()
    for entry in table.get("components", []):
        component = _read_component(entry, context, fail)
        if component.id in seen:
            raise fail(f"declares component '{component.id}' twice {context}")
        seen.add(component.id)
        obj.components.append(component)

    return obj


def _read_component(table: dict, object_context: str, fail) -> PrefabComponent:
    context = f"on a component {object_context}"

    identity = table.get(ID_KEY)
    if not isinstance(identity, str):
        raise fail(f"needs a string '{ID_KEY}' {context}")
    if not document_guid.is_text(identity):
        raise fail(f"holds '{identity}' where '{ID_KEY}' {context} must be a non-empty UUID")
    identity = document_guid.canonical(identity)

    type_name = table.get(TYPE_KEY)
    if type_name is not None and not isinstance(type_name, str):
        raise fail(f"holds a non-string '{TYPE_KEY}' {context}")

    removed = table.get(REMOVED_KEY, False)
    if not isinstance(removed, bool):
        raise fail(f"holds a non-boolean '{REMOVED_KEY}' {context}")

    data = {k: v for k, v in table.items() if k not in RESERVED_KEYS}
    if removed and data:
        # "Removed, and here is its content" is an edit that deleted half of what it meant to.
        raise fail(f"marks a component '{REMOVED_KEY}' but also gives it fields {context}")

    component = PrefabComponent(identity, type_name, data, removed)
    problem = well_known.payload_problem(component)
    if problem is not None:
        raise fail(f"{problem} {context}")

    _normalise_meta_guids(component)
    return component


def _normalise_meta_guids(component: PrefabComponent) -> None:
    """Canonical spelling for every identity the meta payload carries, once, at the boundary, so
    the resolver's minting and every ``==`` in between compare values; already validated as
    guid text by :func:`well_known.payload_problem`."""
    if component.id != well_known.META_ID:
        return
    for key in (well_known.GUID, well_known.PARENT, well_known.TARGET):
        value = component.data.get(key)
        if isinstance(value, str):
            component.data[key] = document_guid.canonical(value)


def _object_table(obj: PrefabObject) -> dict:
    table: dict = {}
    if obj.prefab is not None:
        table["prefab"] = asset_reference.write(obj.prefab)
    if obj.components:
        table["components"] = [_component_table(c) for c in obj.components]
    return table


def _component_table(component: PrefabComponent) -> dict:
    # The reader's shape gate on the way out, so a malformed payload fails at its builder.
    problem = well_known.payload_problem(component)
    if problem is not None:
        raise ValueError(f"this document {problem}, so it cannot be written")

    if not document_guid.is_text(component.id):
        raise ValueError(f"component id '{component.id}' is not a non-empty UUID, so it cannot be written")

    table: dict = {ID_KEY: document_guid.canonical(component.id)}
    if component.type is not None:
        table[TYPE_KEY] = component.type
    if component.removed:
        table[REMOVED_KEY] = True
    _normalise_meta_guids(component)
    for key in component.data:
        # Guards payloads mutated after construction; without it `update` swallows the collision.
        if key in RESERVED_KEYS:
            raise ValueError(
                f"a component payload may not use the reserved key '{key}'; "
                "it would collide with the component's own structure"
            )
    table.update(component.data)
    return table


def _reject_unknown(table: dict, known: frozenset[str], context: str, fail) -> None:
    for key in table:
        if key not in known:
            raise fail(f"has an unknown key '{key}' {context}")


def _validate_parents(parents: dict[str, str | None], fail) -> None:
    """Reject dangling and cyclic parents here rather than as infinite recursion in the loader."""
    for guid, parent in parents.items():
        if parent is not None and parent not in parents:
            raise fail(f"parents object '{guid}' to '{parent}', which does not exist")

    for start in parents:
        slow, current, steps = start, start, 0
        while parents[current] is not None:
            current = parents[current]  # type: ignore[assignment]
            steps += 1
            if steps % 2 == 0:
                slow = parents[slow]  # type: ignore[assignment]
            if current == slow:
                raise fail(f"has a parent cycle through object '{current}'")
