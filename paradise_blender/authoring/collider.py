"""Collider authoring.

Godot supplies typed shape resources (``BoxShape3D``, ``SphereShape3D``, ``CapsuleShape3D``)
attached to ``CollisionShape3D`` nodes, so its exporter just reads the shape's own dimensions.
Blender has no collision primitives at all. So a collider here is an ordinary object -- an
empty or a mesh -- carrying a :class:`ParadiseColliderProperties` group that says which
contract shape it represents.

Dimensions come from one of two sources, chosen by :attr:`ParadiseColliderProperties.size_source`:

* ``BOUNDS`` (default) -- derive from the object's local bounding box. Works with the
  ordinary modelling workflow: box a cube around the thing, mark it a collider, done.
* ``EXPLICIT`` -- type the numbers. Needed when the visual proxy is not the collision volume
  (a low cylinder standing in for a capsule, say) and for reproducing exact values from a
  Godot-authored scene.

Blender's own Rigid Body physics is deliberately *not* read. It looks like the obvious source,
but its collision shapes are approximations chosen for Blender's solver (``CONVEX_HULL``,
``MESH``) and mostly have no contract equivalent, so reading them would silently produce
wrong-sized colliders for anything but the box case.
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

    # Explicit dimensions, in the collider's own local space BEFORE scale folding. Stored in
    # Blender axes; the exporter converts. Only the fields the chosen shape uses are read.
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

    # Godot stores a 20-bit layer mask on the owning body. The contract carries a single layer
    # INDEX, so a multi-bit mask is lossy -- the exporter warns. Authored as an index directly
    # here, which makes the lossy case unrepresentable rather than merely detected.
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
    """Resolve ``(size, radius, height)`` for a collider, in Blender axes and local space.

    Returned values are pre-fold: the caller applies
    :mod:`..contract.collider_fold` with the scale relative to the entity root, then converts
    axes. Doing it in that order matters -- folding after the axis change would pair the box
    extents with the wrong scale components.
    """
    props = obj.paradise_collider

    if props.size_source == "EXPLICIT":
        return (tuple(props.size), props.radius, props.height)

    size = _local_bounds(obj)

    # Sphere: the largest half-extent, so the shape encloses the visual bounds.
    radius_from_bounds = max(size) / 2.0

    # Capsule: Blender Z is the contract's Y once converted, so the capsule axis is Blender Z
    # and its cross-section is X/Y.
    capsule_radius = max(size[0], size[1]) / 2.0
    capsule_height = size[2]

    if props.shape == PhysicsShapeType.SPHERE:
        return (size, radius_from_bounds, 0.0)
    if props.shape == PhysicsShapeType.CAPSULE:
        return (size, capsule_radius, capsule_height)
    return (size, 0.0, 0.0)


def _local_bounds(obj: Object) -> tuple[float, float, float]:
    """Local-space extents of an object.

    ``obj.dimensions`` is world-space (scale already applied), which would double-count scale
    once the fold runs, so the untransformed bounds are used instead.

    Empties need their own branch: ``bound_box`` is all zeros for them, because an empty has no
    geometry -- only a display gizmo. Using it directly would silently export a zero-sized
    collider, which is the natural way to author a box collider (add a Cube empty, size it) and
    would produce a scene where nothing collides with anything.
    """
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
