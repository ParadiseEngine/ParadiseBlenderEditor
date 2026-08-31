"""The components the authoring format itself defines -- the mirror of C# ``WellKnownComponents``.

An object has no privileged members: identity, name, place in the tree and placement are all
components, addressed the same way a game's are. That is what lets a prefab instance override any
of them through one mechanism instead of needing a second syntax for the four fields that used to
be spelled at the object level.

**These ids are FIXED FOREVER.** They are written into every document, and changing one orphans
every object in every scene.
"""

from __future__ import annotations

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
    "payload_problem",
]

#: ``meta`` -- identity, display name, the parent link, and prefab-override addressing.
#:
#: Structure lives here rather than on the transform because a reparent changes what an object IS
#: in the tree, while a transform is only numbers. Keeping them apart means moving an object and
#: re-hanging it are different edits in a diff.
META_ID = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
META_TYPE = "meta"

#: ``transform`` -- the object's LOCAL position, rotation and scale.
#:
#: Deliberately NOT ``Paradise.Export.Data.TransformComponentData``'s id: that component carries a
#: single ``World`` matrix, the baked form, while this is the authoring form the bake flattens
#: against the parent chain. Two field sets under one id is the collision that makes ShiningPie's
#: two GameConfig declarations unmergeable.
TRANSFORM_ID = "7e55c210-3d41-4b8a-8f26-9c0a5e71b4d2"
TRANSFORM_TYPE = "transform"

# ---- meta fields ---------------------------------------------------------------------------

#: The object's identity. Unique per document; prefab-local inside a prefab.
GUID = "Guid"

#: Display name. Diagnostics and readability; not unique, not identity.
NAME = "Name"

#: The parent object's :data:`GUID`, or absent for a root.
PARENT = "Parent"

#: On an object that overrides a prefab CHILD: the prefab-local guid it addresses.
TARGET = "Target"

#: On a :data:`TARGET` carrier: whether that child is dropped, along with its descendants.
#:
#: Spelled ``Dropped``, not ``Removed``, so it does not differ from the component-level ``removed``
#: marker by case alone -- two spellings of one word meaning different things at different levels
#: is a diff nobody reads correctly at a glance.
DROPPED = "Dropped"

# ---- transform fields ----------------------------------------------------------------------

#: Local translation, engine convention (Y-up, metres).
POSITION = "Position"

#: Local rotation as ``[x, y, z, w]``.
ROTATION = "Rotation"

#: Local scale.
SCALE = "Scale"

# ---- shape ---------------------------------------------------------------------------------


def is_meta_field(key: str) -> bool:
    """Whether *key* is a ``meta`` field the format itself defines, as opposed to a
    game-extended payload field riding along in the same table.

    The resolver rebuilds every format-owned field itself -- identity is minted, the parent is
    remapped, and the carrier-only fields describe the override rather than the object -- so
    this is the set it must NOT copy through. Adding a meta field means adding it here, and the
    copy-through loop needs no edit.
    """
    return key in (GUID, NAME, PARENT, TARGET, DROPPED)


def payload_problem(component) -> str | None:
    """The first shape problem in a well-known component's payload, phrased to follow a source
    name -- or ``None`` when there is none, including for a component that is not well-known at
    all. The mirror of C# ``WellKnownComponents.PayloadProblem``.

    A game component's payload is deliberately opaque here -- its shape is a schema question the
    game answers. These two are the components whose schema the FORMAT owns, so the format checks
    them; without this, ``Position = [0.0, 1.5]`` loaded silently as the origin, which is data
    loss dressed as a default.

    ``meta`` stays OPEN -- the resolver carries unknown meta fields through -- so only the fields
    it defines are checked. ``transform`` is CLOSED: nothing reads an unknown field on it and a
    save replaces it wholesale, so an unknown name there is a typo, not an extension.
    """
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
        return f"marks '{META_TYPE}.{DROPPED}' without a '{TARGET}' -- only an override carrier can drop a prefab child"

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


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _is_guid_text(value: object) -> bool:
    """C# ``DocumentGuid.TryParse`` plus the non-empty rule: hyphenated ``8-4-4-4-12`` or
    undashed 32 hex digits, case-insensitive, and not the all-zero guid. Spelled out rather
    than ``uuid.UUID``, which also accepts braces and ``urn:`` forms no tool writes."""
    if not isinstance(value, str):
        return False
    if len(value) == 36:
        if any(value[i] != "-" for i in (8, 13, 18, 23)):
            return False
        digits = value.replace("-", "")
        if len(digits) != 32:
            return False
    elif len(value) == 32:
        digits = value
    else:
        return False
    if any(c not in _HEX_DIGITS for c in digits):
        return False
    return digits != "0" * 32


def _is_number_array(value: object, length: int) -> bool:
    if not isinstance(value, list) or len(value) != length:
        return False
    return all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)
