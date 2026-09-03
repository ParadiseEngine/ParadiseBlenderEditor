"""The format's own components, mirroring C# ``WellKnownComponents``. The ids are FIXED
FOREVER: changing one orphans every object in every scene."""

from __future__ import annotations

from . import guid

__all__ = [
    "DROPPED",
    "GUID",
    "META_ID",
    "META_TYPE",
    "NAME",
    "PARENT",
    "POSITION",
    "ROTATION",
    "SCALE",
    "TARGET",
    "TRANSFORM_ID",
    "TRANSFORM_TYPE",
    "is_meta_field",
    "parent_caption",
    "payload_problem",
]

#: ``meta``: identity, name, parent link, override addressing. The parent lives here, not on
#: the transform, so moving and re-hanging are different edits in a diff.
META_ID = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
META_TYPE = "meta"

#: ``transform``: LOCAL TRS. Not ``TransformComponentData``'s id, which carries a baked World
#: matrix; two field sets under one id is a collision.
TRANSFORM_ID = "7e55c210-3d41-4b8a-8f26-9c0a5e71b4d2"
TRANSFORM_TYPE = "transform"


#: The object's identity. Unique per document; prefab-local inside a prefab.
GUID = "Guid"

#: Display name. Diagnostics and readability; not unique, not identity.
NAME = "Name"

#: The parent object's :data:`GUID`, or absent for a root.
PARENT = "Parent"

#: On an object that overrides a prefab CHILD: the prefab-local guid it addresses.
TARGET = "Target"

#: On a :data:`TARGET` carrier: the child and its descendants are dropped. ``Dropped``, not
#: ``Removed``, so it never differs from the component-level ``removed`` by case alone.
DROPPED = "Dropped"


POSITION = "Position"

#: Local rotation as ``[x, y, z, w]``.
ROTATION = "Rotation"

SCALE = "Scale"


def is_meta_field(key: str) -> bool:
    """Whether *key* is a format-owned ``meta`` field, the set the resolver must NOT copy
    through (it rebuilds them). Adding a meta field means adding it here."""
    return key in (GUID, NAME, PARENT, TARGET, DROPPED)


def parent_caption(guid: str | None, name: str | None) -> str:
    """A parent as the panel writes it: name plus identity, or a root mark."""
    if not guid:
        return "— (root)"
    if name:
        return f"{name}  ({guid})"
    return guid


def payload_problem(component) -> str | None:
    """The first shape problem in a well-known payload, or ``None`` (mirror of C#
    ``PayloadProblem``). Without it ``Position = [0.0, 1.5]`` loaded silently as the origin.
    ``meta`` is open (unknown fields ride along); ``transform`` is closed, since nothing reads an
    unknown field there and a typo would bake as identity."""
    if component.id == META_ID:
        return _meta_problem(component.data)
    if component.id == TRANSFORM_ID:
        return _transform_problem(component.data)
    return None


def _meta_problem(data: dict) -> str | None:
    for key, value in data.items():
        if key in (GUID, PARENT, TARGET) and not _is_guid_text(value):
            return f"needs '{META_TYPE}.{key}' to be a UUID string"
        if key == NAME and not isinstance(value, str):
            return f"needs '{META_TYPE}.{NAME}' to be a string"
        if key == DROPPED and not isinstance(value, bool):
            return f"needs '{META_TYPE}.{DROPPED}' to be a boolean"

    if DROPPED in data and TARGET not in data:
        return (
            f"marks '{META_TYPE}.{DROPPED}' without a '{TARGET}' -- "
            "only an override carrier can drop a prefab child"
        )

    return None


def _transform_problem(data: dict) -> str | None:
    for key, value in data.items():
        if key in (POSITION, SCALE):
            if not _is_number_array(value, 3):
                return f"needs '{TRANSFORM_TYPE}.{key}' to be an array of 3 numbers"
        elif key == ROTATION:
            if not _is_number_array(value, 4):
                return f"needs '{TRANSFORM_TYPE}.{ROTATION}' to be an array of 4 numbers"
        else:
            return (
                f"holds '{key}', which '{TRANSFORM_TYPE}' does not define -- "
                "a misspelled field would otherwise load as the identity, silently"
            )

    return None


def _is_guid_text(value: object) -> bool:
    """A non-empty guid in a spelling C# ``DocumentGuid.TryParse`` accepts (:mod:`guid`)."""
    return guid.is_text(value)


def _is_number_array(value: object, length: int) -> bool:
    if not isinstance(value, list) or len(value) != length:
        return False
    return all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)
