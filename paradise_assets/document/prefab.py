"""The authoring document -- what a ``*.prefab`` holds, whether a game calls it a level or a prop.

The Python mirror of ``src/Paradise.Assets.Documents/PrefabDocument.cs`` and its serializer.

Deliberately NOT the export contract. The contract JSON is a bake -- world matrices, references
resolved to values, no identities -- and this is what the bake is computed FROM, so it keeps
exactly what baking destroys: durable identity, local transforms, parents, and references left as
references.

**An object has no privileged members.** Identity, name, parent and placement are all components
(:mod:`well_known`), addressed the way a game's components are. That is the whole reason a prefab
instance needs one override mechanism: a component it repeats is overridden field by field, and
identity and placement are not special cases.

Reading is strict, matching the C# reader. Writing goes through :mod:`canonical_toml`, so read ->
write is byte-identical for a canonical input -- ``paradise-assets prefab-check`` compares bytes.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field

from . import asset_reference, canonical_toml, well_known
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
    """One component entry: identity, readable name, and a payload that sits flat beside them.

    The payload may not use a reserved key: flattened onto the wire it would collide with the
    entry's own structure, and ``dict.update`` in the writer would swallow the collision
    silently. Refusing at construction makes it a named error at the code that built it.
    (Parsed text cannot collide -- TOML itself rejects a duplicate key.)
    """

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
        meta = self.meta
        if meta is None:
            return None
        value = meta.data.get(key)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def with_meta(guid: str, name: str | None = None, parent: str | None = None) -> "PrefabObject":
        """An object carrying just a meta component -- the shape every caller needs."""
        data: dict = {well_known.GUID: guid}
        if name is not None:
            data[well_known.NAME] = name
        if parent is not None:
            data[well_known.PARENT] = parent
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
        """Applies the one rule a document adds to its object list: exactly one root.

        Exactly one because an instance places exactly one thing, and "which of these several is
        the instance" has no good answer -- the rule Unity prefabs, Godot's PackedScene and Unreal
        blueprints all settle on. It is what lets any document be instantiated into any other
        rather than only a privileged kind.

        A document that instantiates another is FINE, and so is one carrying an override carrier.
        Both were once refused -- the first because the resolver could not recurse, the second
        because carriers were "a scene thing" and this was "a prefab". There is one kind of
        document now.
        """
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
        root = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise fail(f"is not valid TOML ({error})") from error

    # tomllib returns a plain dict for both `x = { … }` and `[x]`, so the model type has to be
    # recovered before anything writes the document back -- otherwise every asset reference would
    # move to a header on the first save and scene-check would call every file non-canonical.
    root = canonical_toml.restore_inline_tables(root)

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
            raise fail(f"has an object at index {index} with no '{well_known.META_TYPE}' component carrying a '{well_known.GUID}'")
        if obj.guid in parents:
            raise fail(f"declares guid '{obj.guid}' twice -- identities must be unique per document")

        parents[obj.guid] = obj.parent
        document.objects.append(obj)

    _validate_parents(parents, fail)

    # Structure first, then the document's own rule. Every read goes through here, so "exactly one
    # root" holds for anything downstream that has a document at all.
    document.validate(source)
    return document


def dumps(document: PrefabDocument) -> str:
    """Render a document as canonical TOML text."""
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
    if not isinstance(identity, str) or not identity:
        raise fail(f"needs a string '{ID_KEY}' {context}")

    type_name = table.get(TYPE_KEY)
    if type_name is not None and not isinstance(type_name, str):
        raise fail(f"holds a non-string '{TYPE_KEY}' {context}")

    removed = table.get(REMOVED_KEY, False)
    if not isinstance(removed, bool):
        raise fail(f"holds a non-boolean '{REMOVED_KEY}' {context}")

    data = {k: v for k, v in table.items() if k not in RESERVED_KEYS}
    if removed and data:
        # "Remove this, and also here is what it should contain" has no meaning, and is almost
        # certainly an edit that deleted only half of what it meant to.
        raise fail(f"marks a component '{REMOVED_KEY}' but also gives it fields {context}")

    component = PrefabComponent(identity, type_name, data, removed)
    problem = well_known.payload_problem(component)
    if problem is not None:
        raise fail(f"{problem} {context}")

    return component


def _object_table(obj: PrefabObject) -> dict:
    table: dict = {}
    if obj.prefab is not None:
        table["prefab"] = asset_reference.write(obj.prefab)
    if obj.components:
        table["components"] = [_component_table(c) for c in obj.components]
    return table


def _component_table(component: PrefabComponent) -> dict:
    # The same shape gate the reader applies, pointed the other way: a tool that builds a
    # malformed well-known payload fails here, not as a document the next read refuses.
    problem = well_known.payload_problem(component)
    if problem is not None:
        raise ValueError(f"this document {problem}, so it cannot be written")

    table: dict = {ID_KEY: component.id}
    if component.type is not None:
        table[TYPE_KEY] = component.type
    if component.removed:
        table[REMOVED_KEY] = True
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
    """Reject dangling and cyclic parents.

    A dangling parent is an edit that deleted an object without reparenting its children; a cycle
    has no world transform at all. Both must fail here rather than as infinite recursion while the
    loader walks the hierarchy.
    """
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
