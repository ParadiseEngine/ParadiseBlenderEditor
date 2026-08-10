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

from ..authoring import entity as authoring
from ..authoring.guid import ensure_entity_guid
from ..contract.schema import (
    AgentComponentData,
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
from .sprite import build_particle_emitter, build_sprite_animation
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

    return LevelEntityData(
        id=obj.name,
        # Mint if the object has never been saved, so a fresh entity never exports the all-zero
        # GUID -- which would collide across every such entity at runtime.
        entity_guid=ensure_entity_guid(obj),
        stable_id=obj.name,
        kind=authoring.resolved_kind(props),
        spawn_phase="LevelStart",
        is_active=props.active_on_load,
        prefab=props.model_path.strip() or None,
        prefab_asset_path=identity.prefab_asset_path,
        prefab_guid=identity.prefab_guid,
        prefab_asset_type=identity.prefab_asset_type,
        nearest_instance_root=identity.nearest_instance_root,
        initial_animation=authoring.resolved_initial_animation(props),
        parent=EntityParentData(id=parent_entity.name) if parent_entity is not None else None,
        local_position=local_position,
        local_rotation=local_rotation,
        local_scale=local_scale,
        local_matrix=local_contract,
        world_matrix=world_contract,
        materials=materials.export_material_slots(obj),
        components=_build_components(obj, paths, meshes),
    )


def _build_components(
    obj: bpy.types.Object, paths: ExportPaths, meshes
) -> EntityComponentsData:
    props = obj.paradise
    components = EntityComponentsData()

    mesh_field = meshes.resolve_mesh_field(obj, paths)
    if mesh_field is not None:
        components.renderable = RenderableComponentData(mesh=mesh_field)
    elif props.model_path.strip():
        # An authored model path that did not resolve still marks the entity as renderable, so
        # the runtime reports a missing mesh rather than silently treating it as invisible.
        components.renderable = RenderableComponentData()

    physics = build_colliders(obj, props.physics_colliders)
    if physics:
        components.collider = ColliderComponentData(colliders=physics)
        components.rigidbody = _build_rigidbody(props)

    # Interaction collider geometry is not forwarded (the contract's interactable component
    # only carries a display name today); presence is enough to flag the component. Matches
    # the Godot host, so both produce the same document for the same scene.
    if build_colliders(obj, props.interaction_colliders):
        components.interactable = EntityInteractableComponentData(display_name=obj.name)

    if props.is_agent:
        components.agent = AgentComponentData(
            move_speed=authoring.resolved_move_speed(props),
            acceleration=authoring.resolved_acceleration(props),
            idle_clip=authoring.resolved_idle_animation(props),
            walk_clip=authoring.resolved_walk_animation(props),
        )

    if props.sprite_enabled:
        components.sprite_animation = build_sprite_animation(obj, paths)

    if props.particle_kind != "NONE":
        components.particle_emitter = build_particle_emitter(obj, paths)

    return components


def _build_rigidbody(props) -> RigidbodyComponentData:
    """Body type follows the authored flags, matching the Godot host's rule.

    Blender's own rigid-body settings are deliberately not read: its solver's body types and
    the contract's do not correspond, and an object can carry Blender physics purely for
    animation baking without being a runtime dynamic body.
    """
    if props.is_dynamic_body:
        body_type = PhysicsBodyType.DYNAMIC
    elif props.is_agent:
        body_type = PhysicsBodyType.KINEMATIC
    else:
        body_type = PhysicsBodyType.STATIC

    return RigidbodyComponentData(
        body_type=body_type,
        # A static or kinematic body has no mass in the contract; carrying the authored value
        # through would let a nonzero mass on a static body read as a solver hint it is not.
        mass=props.body_mass if props.is_dynamic_body else 0.0,
        linear_damping=props.body_linear_damping,
        restitution=props.body_restitution,
        friction=props.body_friction,
        layer=0,
        layer_name="",
    )
