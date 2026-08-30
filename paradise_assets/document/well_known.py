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
