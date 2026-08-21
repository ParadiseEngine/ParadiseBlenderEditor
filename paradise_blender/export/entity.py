"""One Blender object -> ``LevelEntityData``.

Port of ``SceneDataExporter.ExportEntity`` / ``BuildComponents``.

One deliberate deviation from the Godot host: the local transform is computed relative to the
**nearest entity ancestor**, not the immediate parent object. Godot writes the node's own
``Position`` while reporting the nearest ``EntityExport`` ancestor as ``Parent`` -- so when a
plain ``Node3D`` sits between two entities, its local transform and its declared parent
disagree. Blender scenes routinely have such intermediate objects (empties used for rigging or
grouping), so the local transform is made consistent with the declared parent instead. For the
common case where the parent entity *is* the immediate parent, the two agree exactly.
"""

from __future__ import annotations

import bpy

from ..authoring import authored_components
from ..authoring import entity as authoring
from ..authoring.guid import ensure_entity_guid
from ..contract import authoring_router, component_ids
from ..contract.schema import (
    AuthoredComponentData,
    ColliderComponentData,
    EntityComponentsData,
    EntityInteractableComponentData,
    EntityParentData,
    LevelEntityData,
    PhysicsBodyType,
    RenderableComponentData,
    RigidbodyComponentData,
)
from ..paths import ExportPaths
from .collider import build_colliders
from .light import export_light
from .transform import decompose_contract

__all__ = ["export_entity", "find_parent_entity"]


def find_parent_entity(obj: bpy.types.Object) -> bpy.types.Object | None:
    """Nearest ancestor that is itself an exported entity."""
    parent = obj.parent
    while parent is not None:
        if authoring.is_entity(parent):
            return parent
        parent = parent.parent
    return None


def export_entity(
    obj: bpy.types.Object,
    paths: ExportPaths,
    materials,  # MaterialExporter
    meshes,  # MeshExporter
    prefabs,  # PrefabExporter
) -> LevelEntityData:
    props = obj.paradise
    parent_entity = find_parent_entity(obj)

    # Local transform relative to the declared parent entity (see the module docstring).
    if parent_entity is not None:
        local_matrix = parent_entity.matrix_world.inverted_safe() @ obj.matrix_world
    else:
        local_matrix = obj.matrix_world.copy()

    local_position, local_rotation, local_scale, local_contract = decompose_contract(local_matrix)
    _, _, _, world_contract = decompose_contract(obj.matrix_world)

    identity = prefabs.resolve_and_export(obj, paths)

    entity = LevelEntityData(
        id=obj.name,
        # Mint if the object has never been saved, so a fresh entity never exports the all-zero
        # GUID -- which would collide across every such entity at runtime.
        entity_guid=ensure_entity_guid(obj),
        stable_id=obj.name,
        # Identity defaults; an authored paradise.identity component overrides them below.
        kind="Prop",
        spawn_phase="LevelStart",
        is_active=True,
        prefab=props.model_path.strip() or None,
        prefab_asset_path=identity.prefab_asset_path,
        prefab_guid=identity.prefab_guid,
        prefab_asset_type=identity.prefab_asset_type,
        nearest_instance_root=identity.nearest_instance_root,
        initial_animation=None,
        parent=EntityParentData(id=parent_entity.name) if parent_entity is not None else None,
        local_position=local_position,
        local_rotation=local_rotation,
        local_scale=local_scale,
        local_matrix=local_contract,
        world_matrix=world_contract,
        materials=materials.export_material_slots(obj),
        components=_build_components(obj, paths, meshes),
    )
    _apply_authored_components(obj, entity, paths)
    return entity


def _build_components(
    obj: bpy.types.Object, paths: ExportPaths, meshes
) -> EntityComponentsData:
    props = obj.paradise
    components = EntityComponentsData()

    mesh_field = meshes.resolve_mesh_field(obj, paths)
    if mesh_field is not None:
        components.add_engine(component_ids.RENDERABLE, RenderableComponentData(mesh=mesh_field))
    elif props.model_path.strip():
        # An authored model path that did not resolve still marks the entity as renderable, so
        # the runtime reports a missing mesh rather than silently treating it as invisible.
        components.add_engine(component_ids.RENDERABLE, RenderableComponentData())

    physics = build_colliders(obj, props.physics_colliders)
    if physics:
        components.add_engine(component_ids.COLLIDER, ColliderComponentData(colliders=physics))
        # A derived body: a wall, a shelf, a parked car — static, no mass. An authored
        # rigidbody REPLACES this entry, and _apply_authored_components upgrades it to
        # Kinematic when the entity also authors an agent — the rule the old fixed flags
        # (is_dynamic_body / is_agent) used to encode.
        components.add_engine(component_ids.RIGIDBODY, RigidbodyComponentData(
            body_type=PhysicsBodyType.STATIC,
            mass=0.0,
            linear_damping=0.0,
            layer_name="",  # the C# record's default; None would round-trip but diff noisily
        ))

    # Interaction collider geometry is not forwarded (the contract's interactable component
    # only carries a display name today); presence is enough to flag the component. Matches
    # the Godot host, so both produce the same document for the same scene.
    if build_colliders(obj, props.interaction_colliders):
        components.add_engine(
            component_ids.INTERACTABLE, EntityInteractableComponentData(display_name=obj.name))

    if obj.type == "LIGHT":
        # A lamp marked as an entity OWNS its light: it travels as Components.Light (the same
        # entity-owned entry the Godot host authors by pointing at a light) and is left out of
        # the scene-level Lighting state (see scene.py), or the runtime would light it twice.
        # Position and direction are world-space, exactly as the scene-level list carries them.
        components.add_engine(component_ids.LIGHT, export_light(obj))

    return components


def _apply_authored_components(
    obj: bpy.types.Object, entity: LevelEntityData, paths: ExportPaths
) -> None:
    """Put every authored component on the entity.

    One destination now, and identity is the only exception: it is spread onto the entity's own
    fields because it is what the entity IS, not something it has. Everything else — the engine's
    components and a game's alike — is appended to the same list.
    """
    components = entity.components
    authored_engine_ids: set[str] = set()

    for component_id, component_type, payload in authored_components.build_component_payloads(
            obj, paths.data_dir):
        if authoring_router.apply(entity, component_id, payload):
            continue  # identity, spread onto the entity and leaving no entry

        if component_id in component_ids.engine_ids():
            authored_engine_ids.add(component_id)

        entry = AuthoredComponentData(
            id=component_id,
            type=component_type,
            data=authoring_router.normalize(component_id, payload))

        # An authored component REPLACES the entry this host derived for the same id rather than
        # adding a second one — two entries for one component is a document nothing can read
        # sensibly. In practice only the rigidbody gets here: everything else this host derives
        # (renderable, light) is not authorable, and the collider lists are exported from their
        # pointer collections, never from a payload.
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
    if (component_ids.RIGIDBODY not in authored_engine_ids
            and components.find(component_ids.AGENT) is not None):
        derived_body = components.find(component_ids.RIGIDBODY)
        if derived_body is not None:
            derived_body.data["BodyType"] = PhysicsBodyType.KINEMATIC

