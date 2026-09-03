"""The two components the CONTRACT defines (mirror of C# ``WellKnownEntityComponents``):
``meta`` (identity, name, parent) and ``transform`` (LOCAL TRS). Their schemas belong to the
format and no game may extend them. The ids are FIXED FOREVER and must match
:mod:`paradise_assets.document.well_known` exactly.
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

META_ID = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
META_TYPE = "meta"

#: NOT v5's ``TransformComponentData`` id: that carried a baked World matrix, and two field
#: sets under one id is a collision no reader can resolve.
TRANSFORM_ID = "7e55c210-3d41-4b8a-8f26-9c0a5e71b4d2"
TRANSFORM_TYPE = "transform"

GUID = "Guid"

NAME = "Name"

PARENT = "Parent"

POSITION = "Position"

#: Local rotation as ``[x, y, z, w]`` -- NOT Blender's ``(w, x, y, z)``.
ROTATION = "Rotation"

SCALE = "Scale"


def meta_payload(guid: str, name: str, parent: str | None = None) -> dict:
    """The ``meta`` payload. ``Parent`` is OMITTED for a root, not null: TOML has no null."""
    payload = {GUID: guid, NAME: name}
    if parent is not None:
        payload[PARENT] = parent
    return payload


def transform_payload(position, rotation, scale) -> dict:
    """The ``transform`` payload, always all three arrays: a loader reads an ABSENT transform as
    "not anywhere", so a dropped field would make an origin object look unplaced."""
    return {
        POSITION: [float(n) for n in position],
        ROTATION: [float(n) for n in rotation],
        SCALE: [float(n) for n in scale],
    }
