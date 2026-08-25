"""Paradise entity authoring on a Blender object.

The Blender analogue of the Godot host's ``AuthoredEntityNode``. Where Godot marks an entity
by *node type*, Blender has no equivalent -- you cannot subclass ``bpy.types.Object`` -- so an
entity here is any object whose ``object.paradise.is_entity`` flag is set.

**This group holds only what the schema cannot.** It used to mirror the retired
``EntityExport`` node property-for-property (~40 hand-declared fields: kind, agent, sprite,
particles, audio, body); all of that is now authored through the game- and engine-published
authoring schema in the entity panel's Components section (``authored_components.py``) and
routed to the typed contract slots at export (``contract/authoring_router.py``). What remains
is HOST data -- things that are about this Blender object rather than about a component:

* ``is_entity`` -- the mark itself. Entity-ness is not a component; it is whether the object
  exists to the exporter at all.
* ``entity_guid`` -- per-placement identity, minted on save.
* ``model_path`` -- an explicit GLB override for the mesh pipeline. It feeds
  ``MeshExporter.resolve_mesh_field`` and the prefab label, both of which run before any
  component exists; the Godot host keeps ``ModelPath`` on the node for the same reason.
* the two collider lists -- host-object references the exporter BAKES into the collider and
  interactable components, this host's half of the schema's ``authoredBy: shape``.

That difference from Godot has one useful consequence and one trap:

* **Useful:** any object type can be an entity -- a mesh, an empty, a collection instance --
  without a wrapper node in the hierarchy. Godot needs a ``Node3D`` parent per entity.
* **Trap:** the flag is invisible in the outliner. The N-panel shows it, and
  ``ops.paradise_select_entities`` exists so an author can find them all.
"""

from __future__ import annotations

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    IntProperty,
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
    """One object reference held for an arbitrary authored component's host field.

    Plain authored values are stored as
    ID-properties, keyed by component id and field path, so ANY component -- the engine's or a
    game's -- gets storage for free. Object references cannot be: Blender ID-properties hold no
    pointers, so a reference needs a REGISTERED property, and registered properties are declared
    in Python at addon-registration time when no project schema has been loaded yet.

    That asymmetry is what made host references an engine-component privilege: two collections were
    declared by hand and mapped to two known ids, so a game component declaring ``authoredBy: shape``
    was detected off the schema and then had nowhere to put a pointer -- it drew as "not authorable
    in Blender yet". Those two collections are gone; a .blend authored before this re-assigns its
    colliders through the same panel, which now serves every component the same way.

    ONE keyed collection closes that without knowing any component in advance: the key is
    ``<component-id>/<field-path>``, so every host field of every component shares one registered
    property and is told apart by what it says about itself. Nothing here needs the schema at
    registration time, which is the constraint that ruled out registering a collection per field.
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
    """Host-side authoring data for one object -- see the module docstring for why this is
    deliberately small. Component data does not belong here; it belongs in the schema."""

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

    #: EVERY host reference on this object, for every component -- see HostReference. One store,
    #: because two hand-declared ones were what made object references an engine privilege.
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
    """True when the object is marked for export.

    Guards against objects created before the addon registered, whose ``paradise`` group has
    not been initialized.
    """
    props = getattr(obj, "paradise", None)
    return bool(props and props.is_entity)


def entity_objects(scene: bpy.types.Scene) -> list[Object]:
    """Every exportable entity in the scene, in a stable order.

    Sorted by name rather than left in Blender's internal collection order: Blender does not
    guarantee that order across sessions, and an unstable order would make every export
    produce a spuriously different diff.
    """
    return sorted((obj for obj in scene.objects if is_entity(obj)), key=lambda o: o.name)


#: Registered in order; HostReference must precede the group that points at it.
classes = (HostReference, ParadiseEntityProperties)


def register_pointers() -> None:
    Object.paradise = PointerProperty(type=ParadiseEntityProperties)


def unregister_pointers() -> None:
    if hasattr(Object, "paradise"):
        del Object.paradise
