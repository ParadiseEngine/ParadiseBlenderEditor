"""Entity authoring on a Blender object: a flag, since ``bpy.types.Object`` cannot be subclassed.

The property group holds only HOST data (the flag, the stored guid, a model path override, host
references). Do not add fixed component fields back: the ~40-field mirror this replaced is
exactly what a schema change silently drifts away from; components go through the schema. The
flag is invisible in the outliner, which is why ``ops.paradise_select_entities`` exists.
"""

from __future__ import annotations

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Object, PropertyGroup

__all__ = [
    "HostReference",
    "ParadiseEntityProperties",
    "classes",
    "entity_objects",
    "is_entity",
]


class HostReference(PropertyGroup):
    """One object reference for any component's host field, keyed ``<component-id>/<field-path>``.

    ID properties hold no pointers, so a reference needs a REGISTERED property, and those are
    declared before any schema is loaded. One keyed collection serves every component; the two
    hand-declared collections it replaced made host references an engine privilege.
    """

    key: StringProperty(  # type: ignore[valid-type]
        name="Field",
        description="Which component field this reference fills: <component-id>/<field-path>",
        default="",
    )

    target: PointerProperty(  # type: ignore[valid-type]
        type=Object,
        name="Object",
        description="Object whose shape and transform fill this field",
    )


class ParadiseEntityProperties(PropertyGroup):
    """Host-side authoring data; component data belongs in the schema (module docstring)."""

    is_entity: BoolProperty(  # type: ignore[valid-type]
        name="Paradise Entity",
        description="Export this object as a Paradise entity",
        default=False,
    )

    model_path: StringProperty(  # type: ignore[valid-type]
        name="Model",
        description=(
            "Optional explicit GLB under the project data directory. Leave empty to export "
            "this object's own mesh"
        ),
        subtype="FILE_PATH",
        default="",
    )

    #: Every host reference on this object, for every component (see HostReference).
    host_refs: CollectionProperty(type=HostReference)  # type: ignore[valid-type]

    entity_guid: StringProperty(  # type: ignore[valid-type]
        name="Entity GUID",
        description=(
            "Stable per-placement identity, minted on save. Empty until minted. "
            "Duplicating an object clears it so the copy gets its own"
        ),
        default="",
    )


def is_entity(obj: Object) -> bool:
    """Whether the object is marked for export (tolerating a pre-addon object with no group)."""
    props = getattr(obj, "paradise", None)
    return bool(props and props.is_entity)


def entity_objects(scene: bpy.types.Scene) -> list[Object]:
    """Every entity, sorted by name: Blender guarantees no collection order across sessions."""
    return sorted((obj for obj in scene.objects if is_entity(obj)), key=lambda o: o.name)


#: Registered in order; HostReference must precede the group that points at it.
classes = (HostReference, ParadiseEntityProperties)


def register_pointers() -> None:
    Object.paradise = PointerProperty(type=ParadiseEntityProperties)


def unregister_pointers() -> None:
    if hasattr(Object, "paradise"):
        del Object.paradise
