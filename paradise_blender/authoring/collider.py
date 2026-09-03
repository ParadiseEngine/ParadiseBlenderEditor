"""Collider authoring: Blender has no collision primitives, so a collider is an ordinary object
carrying a shape kind, sized from its bounds or typed explicitly. Blender's own Rigid Body
shapes are NOT read: ``CONVEX_HULL``/``MESH`` have no contract equivalent and would silently
produce wrong-sized colliders.
"""

from __future__ import annotations

from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty
from bpy.types import Object, PropertyGroup

from ..contract.schema import PhysicsShapeType

__all__ = [
    "ParadiseColliderProperties",
    "classes",
    "collider_dimensions",
    "is_collider",
    "register_pointers",
    "unregister_pointers",
]

SHAPE_ITEMS = [
    (PhysicsShapeType.BOX, "Box", "Axis-aligned box in the collider's local space"),
    (PhysicsShapeType.SPHERE, "Sphere", "Sphere; radius folds with the largest scale axis"),
    (
        PhysicsShapeType.CAPSULE,
        "Capsule",
        "Y-aligned capsule. Rotate the object to orient it along another axis",
    ),
]

SIZE_SOURCE_ITEMS = [
    ("BOUNDS", "From Bounds", "Derive dimensions from the object's local bounding box"),
    ("EXPLICIT", "Explicit", "Use the dimensions typed below"),
]


class ParadiseColliderProperties(PropertyGroup):
    """Authored collider data for one object."""

    is_collider: BoolProperty(  # type: ignore[valid-type]
        name="Paradise Collider",
        description="Treat this object as a collider shape for its entity",
        default=False,
    )

    shape: EnumProperty(  # type: ignore[valid-type]
        name="Shape", items=SHAPE_ITEMS, default=PhysicsShapeType.BOX
    )

    size_source: EnumProperty(  # type: ignore[valid-type]
        name="Dimensions", items=SIZE_SOURCE_ITEMS, default="BOUNDS"
    )

    # Local space, Blender axes, BEFORE scale folding.
    size: FloatVectorProperty(  # type: ignore[valid-type]
        name="Size",
        description="Box full size (not half-extents)",
        size=3,
        default=(1.0, 1.0, 1.0),
        min=0.0,
        subtype="XYZ",
    )
    radius: FloatProperty(name="Radius", default=0.5, min=0.0)  # type: ignore[valid-type]
    height: FloatProperty(  # type: ignore[valid-type]
        name="Height",
        description="Total capsule height including the caps, matching Godot's CapsuleShape3D",
        default=2.0,
        min=0.0,
    )

    is_trigger: BoolProperty(  # type: ignore[valid-type]
        name="Trigger",
        description=(
            "Sensor volume: detected but not solid. Godot expresses this by parenting the "
            "shape to an Area3D"
        ),
        default=False,
    )

    is_static: BoolProperty(name="Static", default=False)  # type: ignore[valid-type]

    # An INDEX, not Godot's bit mask, so the lossy multi-bit case is unrepresentable.
    layer: IntProperty(  # type: ignore[valid-type]
        name="Layer",
        description="Collision layer index. The runtime rebuilds the mask as 1 << index",
        default=0,
        min=0,
        max=31,
    )


def is_collider(obj: Object) -> bool:
    props = getattr(obj, "paradise_collider", None)
    return bool(props and props.is_collider)


def collider_dimensions(obj: Object) -> tuple[tuple[float, float, float], float, float]:
    """``(size, radius, height)`` in Blender axes, pre-fold. The caller folds scale BEFORE
    converting axes; the other order pairs extents with the wrong scale components."""
    props = obj.paradise_collider

    if props.size_source == "EXPLICIT":
        return (tuple(props.size), props.radius, props.height)

    size = _local_bounds(obj)

    # Sphere: the largest half-extent, so the shape encloses the visual bounds.
    radius_from_bounds = max(size) / 2.0

    # Capsule axis is Blender Z (contract Y after conversion).
    capsule_radius = max(size[0], size[1]) / 2.0
    capsule_height = size[2]

    if props.shape == PhysicsShapeType.SPHERE:
        return (size, radius_from_bounds, 0.0)
    if props.shape == PhysicsShapeType.CAPSULE:
        return (size, capsule_radius, capsule_height)
    return (size, 0.0, 0.0)


def _local_bounds(obj: Object) -> tuple[float, float, float]:
    """Local extents. Not ``obj.dimensions`` (world-space; the fold would double-count scale),
    and an empty's ``bound_box`` is all zeros, which would silently export a zero-sized collider."""
    if obj.type == "EMPTY":
        # A cube/sphere empty of display size s spans -s..s on each axis, so its extent is 2s.
        extent = obj.empty_display_size * 2.0
        return (extent, extent, extent)

    corners = [tuple(corner) for corner in obj.bound_box]
    if not corners:
        return (0.0, 0.0, 0.0)

    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    zs = [c[2] for c in corners]
    return (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))


classes = (ParadiseColliderProperties,)


def register_pointers() -> None:
    from bpy.props import PointerProperty

    Object.paradise_collider = PointerProperty(type=ParadiseColliderProperties)


def unregister_pointers() -> None:
    if hasattr(Object, "paradise_collider"):
        del Object.paradise_collider
