"""One Blender object -> its authored components, without its placement.

Port of ``SceneDataExporter.ExportEntity`` / ``BuildComponents`` against schema v6: an object in
the document IS a list of ``{Id, Type, Data}`` components and nothing else. Identity and placement
are components too -- the format's own ``meta`` (guid, name, parent guid) and ``transform``
(LOCAL position, rotation, scale) -- but they are NOT built here. A local transform names a
parent, and the parent is the nearest ancestor that is itself exported, which cannot be known per
object: an author may park things under an empty that authors nothing. :mod:`.placement`
resolves that once over the whole exported set, after this module has decided which objects
survive.

An object that authors nothing is therefore not exported at all. A positioned empty with no
components would be a row that says "an author marked this and then said nothing about it",
which no runtime can act on.
"""

from __future__ import annotations

import bpy

from ..authoring import authored_components
from ..contract import authoring_router, component_ids
from ..contract.schema import (
    AuthoredComponentData,
    ColliderComponentData,
    EntityComponentsData,
    MaterialsComponentData,
    PhysicsBodyType,
    RigidbodyComponentData,
)
from ..paths import ExportPaths
from .collider import build_colliders
from .light import export_light

__all__ = ["export_entity"]


def export_entity(
    obj: bpy.types.Object,
    paths: ExportPaths,
    materials,  # MaterialExporter
    meshes,  # MeshExporter
) -> EntityComponentsData | None:
    """This object's components, or None when it authors nothing worth a row in the document.

    "Nothing" still means nothing BEYOND the identity and the placement this host writes for every
    object it emits. An empty an author marked as an entity and never gave a mesh, a collider or a
    component is not a statement about the world; the runtime would build an entity with no shape
    for it and then have nothing to do with it.

    **The placement is no longer added here**, so the emptiness test is simply "did anything get
    added" rather than "more than the two this host prepends". Since v6 an object's transform is
    LOCAL and names a parent, so it cannot be built until the export knows which objects are being
    written -- an object may hang from an ancestor that authors nothing and is therefore absent
    from the document. :class:`.placement.Placement` answers that once, over the whole set, and
    the scene walk applies it to the survivors.
    """
    components = EntityComponentsData(data_dir=paths.data_dir)

    _build_derived(obj, paths, materials, meshes, components)
    _apply_authored_components(obj, components, paths, meshes)

    return components if components.components else None


def _build_derived(
    obj: bpy.types.Object,
    paths: ExportPaths,
    materials,  # MaterialExporter
    meshes,  # MeshExporter
    components: EntityComponentsData,
) -> None:
    """The components this host reads off the Blender object rather than off a form.

    These are as authored as anything in the Components panel — an author draws a mesh and a
    collider with Blender's own tools, which is exactly what "authored" means for geometry. They
    are derived here because the alternative is an object slot pointing at the object's own data.
    """
    # NO RENDERABLE. It used to be derived here — "this object has mesh data, so it draws" — which
    # made drawing something an exporter inferred rather than something an author said, and left a
    # game with no way to distinguish a prop's mesh from a skinned actor's. Both are components an
    # author attaches now, pointing at the object whose geometry to export (`authoredBy: mesh`),
    # and _apply_authored_components bakes them.
    #
    # The SLOTS are still derived, and that asymmetry is the point: which GLB an object draws is a
    # decision, and which materials override its primitives is Blender's material stack. There is
    # nothing for a picker to add, so the exporter reads it — the same standing the name and the
    # transform have.
    slots = materials.export_material_slots(obj)
    if slots:
        components.add_engine(component_ids.MATERIALS, MaterialsComponentData(slots=slots))

    # The collider component's own references, resolved off the SCHEMA rather than a collection
    # named in Python. This is the last dedicated collider path, and it is not here for storage: a
    # collider list also DERIVES a static rigidbody below, which is a rule about physics.
    physics = build_colliders(obj, authored_components.collider_entries(obj, paths.data_dir))
    if physics:
        components.add_engine(component_ids.COLLIDER, ColliderComponentData(colliders=physics))
        # A derived body: a wall, a shelf, a parked car — static, no mass. An authored
        # rigidbody REPLACES this entry, and _apply_authored_components upgrades it to
        # Kinematic when the object also authors an agent — the rule the old fixed flags
        # (is_dynamic_body / is_agent) used to encode.
        components.add_engine(component_ids.RIGIDBODY, RigidbodyComponentData(
            body_type=PhysicsBodyType.STATIC,
            mass=0.0,
            linear_damping=0.0,
            layer_name="",  # the C# record's default; None would round-trip but diff noisily
        ))

    if obj.type == "LIGHT":
        # A lamp marked as an entity owns its light here rather than being emitted again by the
        # scene walk, which skips it for exactly that reason (see scene.py). Position and
        # direction are world-space, as the contract carries them.
        components.add_engine(component_ids.LIGHT, export_light(obj))


def _apply_authored_components(
    obj: bpy.types.Object,
    components: EntityComponentsData,
    paths: ExportPaths,
    meshes,  # MeshExporter
) -> None:
    """Put every authored component on the object.

    ONE destination now, and no exceptions. Identity used to be spread onto the entity's own
    fields because it was what the entity WAS rather than something it had; there are no fields
    left to spread onto, and the record went with them.
    """
    authored_ids: set[str] = set()

    for component_id, component_type, payload in authored_components.build_component_payloads(
            obj, paths.data_dir, paths, meshes):
        authored_ids.add(component_id)

        entry = AuthoredComponentData(
            id=component_id,
            type=component_type,
            data=authoring_router.normalize(component_id, payload))

        # An authored component REPLACES the entry this host derived for the same id rather than
        # adding a second one — two entries for one component is a document nothing can read
        # sensibly. In practice only the rigidbody gets here: everything else this host derives
        # (name, transform, renderable, light) is not authorable, and the collider lists are
        # exported from their pointer collections, never from a payload.
        existing = components.find(component_id)
        if existing is not None:
            components.components[components.components.index(existing)] = entry
        else:
            components.add(entry)

    # An agent stands on the navmesh and is moved by the simulation, so its DERIVED body is
    # kinematic, not static — unless the author said otherwise with a rigidbody component.
    #
    # A scan rather than two slot reads: the rule wants the entry this host synthesized, and an
    # authored rigidbody is a different entry it must not touch.
    if (component_ids.RIGIDBODY not in authored_ids
            and components.find(component_ids.AGENT) is not None):
        derived_body = components.find(component_ids.RIGIDBODY)
        if derived_body is not None:
            derived_body.data["BodyType"] = PhysicsBodyType.KINEMATIC
