"""A document object shown as a Blender COLLECTION.

The format needs no collection concept and does not get one. A group IS an ordinary object --
``meta`` and ``transform`` and nothing else -- and the things in it are its children. Blender is
the only side that knows the difference, so this module is the ONE definition of which objects
that describes: load and save disagreeing about it would not error, it would move objects between
the scene and a collection on every round trip and rewrite the document each time.

Four rules the shape alone does not settle, each with a reason it cannot go the other way:

- **Never the root.** The root IS the document -- it is what an instance places, what
  ``instancing`` parents new objects under, and what extraction refuses to take. A collection is
  none of those things.
- **Never an instance.** An object with a ``prefab`` reference is a thing, not a grouping, even
  though its resolved root carries only meta and transform.
- **Identity transform only.** A Blender collection cannot be moved, so a group with a placement
  would lose it on the first save. Such an object stays an Empty, which is exactly what an author
  who wants a movable group should use.
- **At least one child.** Otherwise every marker empty in every document silently becomes an
  empty collection. The cost is that emptying a collection in Blender and saving turns it back
  into an Empty; the benefit is that nothing an author already authored changes meaning.
"""

from __future__ import annotations

import bpy

from ..document import well_known

__all__ = ["GUID_KEY", "NAME_KEY", "SHOWN_NAME_KEY", "guid_of", "is_group_entry", "name_of", "tag"]

#: The same key an object carries, so ``store.guid_of`` answers for either.
GUID_KEY = "paradise_guid"

#: The document's ``meta.Name``, and the name Blender showed for it at load. Two keys for the
#: same reason objects need two (#32): Blender uniquifies collection names in one namespace, so
#: ``collection.name`` alone cannot say whether the AUTHOR renamed anything.
NAME_KEY = "paradise_name"
SHOWN_NAME_KEY = "paradise_shown_name"

_IDENTITY = {
    well_known.POSITION: (0.0, 0.0, 0.0),
    well_known.ROTATION: (0.0, 0.0, 0.0, 1.0),
    well_known.SCALE: (1.0, 1.0, 1.0),
}

#: Components a group may carry, and no others.
_OWNED = frozenset({well_known.META_ID.lower(), well_known.TRANSFORM_ID.lower()})


def is_group_entry(entry, *, is_root: bool, has_children: bool) -> bool:
    """Whether this document object should be shown as a Blender collection."""
    if is_root or not has_children or entry.prefab is not None:
        return False
    if not {component.id.lower() for component in entry.components} <= _OWNED:
        return False
    return _is_identity(entry.component(well_known.TRANSFORM_ID))


def _is_identity(transform) -> bool:
    """A missing transform is the identity; so is one that spells it out."""
    if transform is None:
        return True
    for key, expected in _IDENTITY.items():
        value = transform.data.get(key)
        if value is None:
            continue
        if len(value) != len(expected) or any(
            abs(float(a) - b) > 1e-9 for a, b in zip(value, expected, strict=True)
        ):
            return False
    return True


def tag(collection: bpy.types.Collection, guid: str, name: str | None) -> None:
    """Record which document object this collection stands for, and the name it was shown as."""
    collection[GUID_KEY] = guid
    if name is None:
        collection.pop(NAME_KEY, None)
    else:
        collection[NAME_KEY] = name
    collection[SHOWN_NAME_KEY] = collection.name


def guid_of(collection: bpy.types.Collection) -> str | None:
    """The document identity of ``collection``, or ``None`` when it is Blender's own."""
    value = collection.get(GUID_KEY)
    return value if isinstance(value, str) and value else None


def name_of(collection: bpy.types.Collection) -> str | None:
    """What ``meta.Name`` should say: the author's rename when there was one, else the authored
    name untouched -- ``Group.001`` is Blender's spelling, not the author's, and the format
    allows two objects one name. The same comparison ``store.document_name`` makes for objects.
    """
    shown = collection.get(SHOWN_NAME_KEY)
    if isinstance(shown, str) and shown == collection.name:
        authored = collection.get(NAME_KEY)
        return authored if isinstance(authored, str) else None
    return collection.name
