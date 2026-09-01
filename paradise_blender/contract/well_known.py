"""The two components the CONTRACT itself defines -- the mirror of C#
``Paradise.Export.Data.WellKnownEntityComponents``.

Since contract v6 the engine declares no authored components at all: every record that used to
ship with it (Renderable, Collider, Materials, Light, Environment, ...) is a GAME declaration now,
read out of the schema a launcher dumps. These two are the exception, and they are not records
anywhere -- not in the engine, not in a game. Their schemas belong to the FORMAT, every host
writes them for every object, and no game may add a field or rename one.

**What changed from v5, and why it is more than a rename.** v5 wrote two engine records:
``NameComponentData`` (a display name) and ``TransformComponentData`` (a flattened WORLD matrix in
the contract's column-vector layout). v6 writes ``meta`` and ``transform``: identity, name and a
PARENT link, plus the object's LOCAL position, rotation and scale.

So the hierarchy survives export again. It did not in v5 -- an object's placement was stated in
world space once and the parent link was dropped outright -- because the engine's bake flattened
the tree anyway. Since v6 the bake is a passthrough and composing the chain is the LOADER's, so
this host has to say what the tree IS rather than pre-multiplying it away.

**These ids are FIXED FOREVER**, and they match :mod:`paradise_assets.document.well_known`
exactly: the two addons write different formats but the same two well-known components, and a
document that disagreed about either id is one neither could read.
"""

from __future__ import annotations

__all__ = [
    "GUID",
    "META_ID",
    "META_TYPE",
    "NAME",
    "PARENT",
    "POSITION",
    "ROTATION",
    "SCALE",
    "TRANSFORM_ID",
    "TRANSFORM_TYPE",
    "meta_payload",
    "transform_payload",
]

#: ``meta`` -- identity, display name and the parent link.
META_ID = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
META_TYPE = "meta"

#: ``transform`` -- the object's LOCAL position, rotation and scale.
#:
#: Deliberately NOT v5's ``TransformComponentData`` id (``5b1a2ea9-...``): that component carried
#: one baked ``World`` matrix, and two different field sets under one id is a collision no reader
#: can resolve. A v5 document therefore does not half-load as v6 -- it fails the schema gate,
#: which is the honest outcome and what the engine's own message says.
TRANSFORM_ID = "7e55c210-3d41-4b8a-8f26-9c0a5e71b4d2"
TRANSFORM_TYPE = "transform"

#: The object's identity. Unique per document, and what :data:`PARENT` addresses.
GUID = "Guid"

#: Display name. Diagnostics and lookup; nothing addresses an object by it.
NAME = "Name"

#: The parent object's :data:`GUID`, absent for a root.
PARENT = "Parent"

#: Local translation, contract convention (right-handed, Y-up, metres).
POSITION = "Position"

#: Local rotation as ``[x, y, z, w]`` -- NOT Blender's ``(w, x, y, z)``.
ROTATION = "Rotation"

#: Local scale.
SCALE = "Scale"


def meta_payload(guid: str, name: str, parent: str | None = None) -> dict:
    """The ``meta`` payload for one object.

    ``Parent`` is OMITTED rather than written null for a root. The contract's readers give an
    absent key the member's default, so an absent parent and a null one are the same statement
    said two ways -- and one of them survives both the JSON and the TOML form, which has no null
    at all.
    """
    payload = {GUID: guid, NAME: name}
    if parent is not None:
        payload[PARENT] = parent
    return payload


def transform_payload(position, rotation, scale) -> dict:
    """The ``transform`` payload: three arrays, always all three.

    Nothing is omitted even at the identity. A transform is the one component whose ABSENCE means
    something in v6 -- a loader reads "no transform" as "this object is not anywhere", which is
    what a camera and a directional light are -- so a partially written one would make an object
    at the origin indistinguishable from an unplaced one for whichever field was dropped.
    """
    return {
        POSITION: [float(n) for n in position],
        ROTATION: [float(n) for n in rotation],
        SCALE: [float(n) for n in scale],
    }
