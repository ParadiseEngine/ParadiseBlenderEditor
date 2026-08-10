"""Collider objects -> ``ColliderShapeData``.

Follows the contract's folding rule (see :mod:`..contract.collider_fold`): the shape is
expressed in the **entity root's local space with the collider's relative scale folded into
its dimensions**, while the root's own scale stays in the entity's ``WorldMatrix``.

Order of operations matters and is easy to invert:

1. compute the collider's scale **relative to the entity root**, in Blender axes
2. fold that into the shape dimensions, in Blender axes
3. convert the resulting dimensions and the pose into contract axes

Folding after the axis change would pair a box's X extent with the wrong scale component,
because the basis change permutes Y and Z.
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
        # Root-exclusive, matching the Godot/Unity convention: empty when the collider IS the
        # entity root. Blender has no node paths, so the object name is the addressable id.
        path="" if collider is entity else collider.name,
        is_trigger=props.is_trigger,
        is_static=props.is_static,
        layer=props.layer,
        layer_name="",
        shape_type=props.shape,
    )

    if props.shape == PhysicsShapeType.BOX:
        folded = collider_fold.box_size(size, relative)
        # Convert extents, then take absolute values: the basis change negates one axis, and a
        # box extent is a magnitude, not a signed offset.
        converted = _convert_extent(folded)
        data.size = converted
    elif props.shape == PhysicsShapeType.SPHERE:
        data.radius = collider_fold.sphere_radius(radius, relative)
    elif props.shape == PhysicsShapeType.CAPSULE:
        # The contract's capsule is Y-aligned. In Blender axes that is Z, so the fold helpers
        # -- which are written in contract axes -- need the relative scale converted first.
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
    """Permute a Blender-axis magnitude triple into contract axes.

    ``(x, y, z) -> (x, z, y)`` -- the same permutation the basis change applies, but without
    the sign flip, because these are unsigned extents rather than positions.
    """
    return (abs(extent[0]), abs(extent[2]), abs(extent[1]))
