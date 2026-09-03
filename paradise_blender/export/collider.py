"""Collider objects -> ``ColliderShapeData``, in the entity root's local space with the
collider's relative scale folded in (:mod:`..contract.collider_fold`). Fold in Blender axes
FIRST, then convert: folding after the basis change pairs extents with the wrong scale components.
"""

from __future__ import annotations

import bpy

from .. import log
from ..authoring.collider import collider_dimensions, is_collider
from ..contract import collider_fold
from ..contract.schema import ColliderShapeData, PhysicsShapeType
from .transform import decompose_contract

__all__ = ["build_colliders", "export_shape"]


def build_colliders(entity: bpy.types.Object, references) -> list[ColliderShapeData]:
    """Export one of an entity's collider lists (physics or interaction)."""
    shapes: list[ColliderShapeData] = []
    for reference in references:
        target = reference.target
        if target is None:
            continue
        if not is_collider(target):
            log.warn(
                f"Entity '{entity.name}' references '{target.name}' as a collider, but that "
                "object is not marked as a Paradise collider. Skipping it."
            )
            continue
        shape = export_shape(entity, target)
        if shape is not None:
            shapes.append(shape)
    return shapes


def export_shape(entity: bpy.types.Object, collider: bpy.types.Object) -> ColliderShapeData | None:
    """Build one collider shape, in the entity root's local space."""
    props = collider.paradise_collider

    entity_scale = tuple(entity.matrix_world.to_scale())
    collider_scale = tuple(collider.matrix_world.to_scale())
    relative = collider_fold.relative_scale(collider_scale, entity_scale)

    size, radius, height = collider_dimensions(collider)

    data = ColliderShapeData(
        id=collider.name,
        # Empty when the collider IS the root (Godot/Unity convention).
        path="" if collider is entity else collider.name,
        is_trigger=props.is_trigger,
        is_static=props.is_static,
        layer=props.layer,
        layer_name="",
        shape_type=props.shape,
    )

    if props.shape == PhysicsShapeType.BOX:
        folded = collider_fold.box_size(size, relative)
        # abs after converting: the basis change negates one axis, and an extent is a magnitude.
        converted = _convert_extent(folded)
        data.size = converted
    elif props.shape == PhysicsShapeType.SPHERE:
        data.radius = collider_fold.sphere_radius(radius, relative)
    elif props.shape == PhysicsShapeType.CAPSULE:
        # The fold helpers are written in contract axes; convert the scale first.
        contract_relative = _convert_extent(relative)
        data.radius = collider_fold.capsule_radius(radius, contract_relative)
        data.height = collider_fold.capsule_height(height, contract_relative)
    else:
        log.warn(f"Collider '{collider.name}' has unsupported shape '{props.shape}'; skipped.")
        return None

    # Pose relative to the entity root, converted into contract axes.
    root_local = entity.matrix_world.inverted_safe() @ collider.matrix_world
    position, rotation, _scale, _matrix = decompose_contract(root_local)
    data.local_center = position
    data.local_rotation = rotation

    return data


def _convert_extent(extent) -> tuple[float, float, float]:
    """``(x, y, z) -> (x, z, y)``: the basis permutation without the sign flip, for magnitudes."""
    return (abs(extent[0]), abs(extent[2]), abs(extent[1]))
