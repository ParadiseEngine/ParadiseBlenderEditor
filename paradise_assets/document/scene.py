"""The authoring scene document -- what a ``*.scene`` file holds.

The Python mirror of ``src/Paradise.Assets.Documents/SceneDocument.cs`` and its serializer.
Deliberately NOT the export contract: the contract JSON is a bake (world matrices, references
resolved to values, no identities), and this is what the bake is computed FROM. So it keeps
exactly what baking destroys -- object GUIDs, local transforms, parents, and references left as
references.

Reading is **strict**, matching the C# reader: unknown keys, malformed GUIDs, duplicate
identities and dangling or cyclic parents are errors naming the object, never a silent skip. The
document is committed source of truth, and a reader that guessed would turn an authoring typo
into a scene that loads and is quietly wrong.

Writing goes through :mod:`.canonical_toml`, so read -> write is byte-identical for a canonical
input. That is not a nicety: ``paradise-assets scene-check`` compares bytes, so an addon that
reformatted on save would put every scene it touched into the diff.

Component payloads are **opaque** to this module. Their schema belongs to the game, and this
addon passes them through untouched -- which is what makes a component it has never heard of
survive a round trip.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field

from . import canonical_toml

__all__ = [
    "SceneComponent",
    "SceneDocument",
    "SceneDocumentError",
    "SceneObject",
    "SceneTransform",
    "SUPPORTED_SCHEMA_VERSION",
    "loads",
    "dumps",
]

#: The only ``schema_version`` this addon reads or writes.
SUPPORTED_SCHEMA_VERSION = 1

_DOCUMENT_KEYS = frozenset({"schema_version", "objects"})
_OBJECT_KEYS = frozenset({"guid", "name", "parent", "transform", "components"})
_TRANSFORM_KEYS = frozenset({"position", "rotation", "scale"})
_COMPONENT_KEYS = frozenset({"id", "type", "data"})

#: No translation, no rotation, unit scale -- what an omitted transform means. Written back as
#: an omitted transform too, so a freshly minted object stays one line in a diff.
IDENTITY = ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0))


class SceneDocumentError(Exception):
    """A scene document could not be read, parsed, or validated."""

    def __init__(self, source: str, problem: str) -> None:
        super().__init__(f"Scene document '{source}' {problem}.")
        self.source = source


@dataclass
class SceneTransform:
    """A local TRS, in engine convention: Y-up, meters, quaternion as ``(x, y, z, w)``."""

    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)

    def is_identity(self) -> bool:
        return (self.position, self.rotation, self.scale) == IDENTITY


@dataclass
class SceneComponent:
    """One component entry: the contract's ``{Id, Type, Data}`` triple in authoring form."""

    id: str
    type: str | None = None
    #: The authored payload, an open table owned by the game's schema. Opaque here.
    data: dict = field(default_factory=dict)


@dataclass
class SceneObject:
    """One authored object: identity, placement, and its component entries."""

    guid: str
    name: str
    parent: str | None = None
    transform: SceneTransform = field(default_factory=SceneTransform)
    #: In document order. Order is data -- the runtime applies components in it.
    components: list[SceneComponent] = field(default_factory=list)


@dataclass
class SceneDocument:
    """The document's objects, in document order."""

    objects: list[SceneObject] = field(default_factory=list)

    def by_guid(self) -> dict[str, SceneObject]:
        return {obj.guid: obj for obj in self.objects}


def loads(text: str, source: str = "<scene>") -> SceneDocument:
    """Parse and validate a scene document."""

    def fail(problem: str) -> SceneDocumentError:
        return SceneDocumentError(source, problem)

    try:
        root = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise fail(f"is not valid TOML ({error})") from error

    _reject_unknown(root, _DOCUMENT_KEYS, "at the document root", fail)

    version = root.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise fail(f"must declare 'schema_version = {SUPPORTED_SCHEMA_VERSION}'")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise fail(
            f"declares schema_version = {version}, which this build cannot read "
            f"(supported: {SUPPORTED_SCHEMA_VERSION})"
        )

    document = SceneDocument()
    parents: dict[str, str | None] = {}
    for index, table in enumerate(root.get("objects", [])):
        obj = _read_object(table, index, fail)
        if obj.guid in parents:
            raise fail(f"declares guid '{obj.guid}' twice -- identities must be unique per document")
        parents[obj.guid] = obj.parent
        document.objects.append(obj)

    _validate_parents(parents, fail)
    return document


def dumps(document: SceneDocument) -> str:
    """Render a document as canonical TOML text."""
    root: dict = {"schema_version": SUPPORTED_SCHEMA_VERSION}
    if document.objects:
        root["objects"] = [_object_table(obj) for obj in document.objects]
    return canonical_toml.dumps(root)


def _read_object(table: dict, index: int, fail) -> SceneObject:
    context = f"on objects[{index}]"
    _reject_unknown(table, _OBJECT_KEYS, context, fail)

    guid = table.get("guid")
    if not isinstance(guid, str) or not _is_guid(guid):
        raise fail(f"holds {guid!r} where 'guid' {context} must be a non-empty UUID")

    name = table.get("name")
    if not isinstance(name, str) or not name:
        raise fail(f"needs a non-empty 'name' {context}")

    context = f"on object '{guid}'"
    obj = SceneObject(guid=guid, name=name)

    parent = table.get("parent")
    if parent is not None:
        if not isinstance(parent, str) or not _is_guid(parent):
            raise fail(f"holds {parent!r} where 'parent' {context} must be a non-empty UUID")
        obj.parent = parent

    transform = table.get("transform")
    if transform is not None:
        obj.transform = _read_transform(transform, context, fail)

    seen: set[str] = set()
    for entry in table.get("components", []):
        component = _read_component(entry, context, fail)
        if component.id in seen:
            raise fail(f"declares component '{component.id}' twice {context}")
        seen.add(component.id)
        obj.components.append(component)

    return obj


def _read_transform(table: dict, context: str, fail) -> SceneTransform:
    context = f"in the transform {context}"
    _reject_unknown(table, _TRANSFORM_KEYS, context, fail)
    return SceneTransform(
        position=_floats(table, "position", 3, context, fail),
        rotation=_floats(table, "rotation", 4, context, fail),
        scale=_floats(table, "scale", 3, context, fail),
    )


def _read_component(table: dict, object_context: str, fail) -> SceneComponent:
    context = f"on a component {object_context}"
    _reject_unknown(table, _COMPONENT_KEYS, context, fail)

    identity = table.get("id")
    if not isinstance(identity, str) or not _is_guid(identity):
        raise fail(f"holds {identity!r} where 'id' {context} must be a non-empty UUID")

    type_name = table.get("type")
    if type_name is not None and not isinstance(type_name, str):
        raise fail(f"holds a non-string 'type' {context}")

    data = table.get("data", {})
    if not isinstance(data, dict):
        raise fail(f"holds a non-table 'data' {context}")

    return SceneComponent(id=identity, type=type_name, data=data)


def _object_table(obj: SceneObject) -> dict:
    """One object as a canonical table. Key order here IS the document's key order."""
    table: dict = {"guid": obj.guid, "name": obj.name}
    if obj.parent is not None:
        table["parent"] = obj.parent

    # An identity transform is omitted on write and defaulted on read, matching the C# writer.
    if not obj.transform.is_identity():
        table["transform"] = {
            "position": [float(v) for v in obj.transform.position],
            "rotation": [float(v) for v in obj.transform.rotation],
            "scale": [float(v) for v in obj.transform.scale],
        }

    if obj.components:
        table["components"] = [_component_table(c) for c in obj.components]
    return table


def _component_table(component: SceneComponent) -> dict:
    table: dict = {"id": component.id}
    if component.type is not None:
        table["type"] = component.type
    if component.data:
        table["data"] = component.data
    return table


def _reject_unknown(table: dict, known: frozenset[str], context: str, fail) -> None:
    for key in table:
        if key not in known:
            raise fail(f"has an unknown key '{key}' {context}")


def _floats(table: dict, key: str, count: int, context: str, fail) -> tuple[float, ...]:
    value = table.get(key)
    if not isinstance(value, list) or len(value) != count:
        raise fail(f"needs '{key}' as an array of {count} numbers {context}")
    for element in value:
        if isinstance(element, bool) or not isinstance(element, (int, float)):
            raise fail(f"holds a non-number in '{key}' {context}")
    return tuple(float(v) for v in value)


def _is_guid(text: str) -> bool:
    """Canonical hyphenated form, or the undashed 32-digit form the C# reader also accepts."""
    if len(text) == 36:
        parts = text.split("-")
        return len(parts) == 5 and [len(p) for p in parts] == [8, 4, 4, 4, 12] and all(
            _is_hex(p) for p in parts
        )
    return len(text) == 32 and _is_hex(text)


def _is_hex(text: str) -> bool:
    return all(c in "0123456789abcdefABCDEF" for c in text)


def _validate_parents(parents: dict[str, str | None], fail) -> None:
    """Reject dangling and cyclic parents.

    A dangling parent is an edit that deleted an object without reparenting its children; a cycle
    has no world transform at all. Both must fail here rather than as infinite recursion while
    the loader walks the hierarchy.
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
