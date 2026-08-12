"""Paradise entity authoring on a Blender object.

The Blender analogue of ``ParadiseGodotEditor``'s ``EntityExport`` node. Where Godot marks an
entity by *node type* (attach the ``EntityExport`` script), Blender has no equivalent -- you
cannot subclass ``bpy.types.Object``. So an entity here is any object whose
``object.paradise.is_entity`` flag is set, with the authored data living in a
:class:`ParadiseEntityProperties` group stored on the object.

That difference has one useful consequence and one trap:

* **Useful:** any object type can be an entity -- a mesh, an empty, a collection instance --
  without a wrapper node in the hierarchy. Godot needs a ``Node3D`` parent per entity.
* **Trap:** the flag is invisible in the outliner. The N-panel shows it, and
  ``ops.paradise_select_entities`` exists so an author can find them all.

The field set mirrors ``EntityExport`` property-for-property, defaults included, so a scene
authored in either tool exports the same contract values. Anything absent here is absent
there too.
"""

from __future__ import annotations

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Object, PropertyGroup

from . import defaults

__all__ = [
    "ColliderReference",
    "ParadiseEntityProperties",
    "classes",
    "entity_objects",
    "is_entity",
    "resolved_acceleration",
    "resolved_idle_animation",
    "resolved_initial_animation",
    "resolved_kind",
    "resolved_move_speed",
    "resolved_walk_animation",
]


# Free-form in the contract (it is just a label), but a dropdown of the common values keeps
# authoring consistent. "Custom" reveals the free-text field -- Blender enums cannot be
# open-ended the way Godot's EnumSuggestion hint is.
KIND_ITEMS = [
    ("Prop", "Prop", "Static or dynamic scenery"),
    ("Character", "Character", "An animated, usually agent-driven entity"),
    ("Door", "Door", "An interactable that opens"),
    ("Trigger", "Trigger", "A non-visual volume"),
    ("Pickup", "Pickup", "A collectable"),
    ("CUSTOM", "Custom…", "Type a custom kind label"),
]

PARTICLE_KIND_ITEMS = [
    # "None" is authoring-only: the contract enum has no None, absence is the null component.
    ("NONE", "None", "No particle emitter is exported"),
    ("Sprite", "Sprite", "Camera-facing flipbook quads (2D particles)"),
    ("Voxel", "Voxel", "Solid cubes (3D particles)"),
]


class ColliderReference(PropertyGroup):
    """One entry in an entity's physics or interaction collider list.

    Godot stores these as ``NodePath`` arrays into the scene tree. Blender has no node paths,
    so this holds a direct object pointer -- which is strictly better: it survives renames and
    Blender maintains the reference itself.
    """

    target: PointerProperty(  # type: ignore[valid-type]
        type=Object,
        name="Collider",
        description="Object whose shape and transform define this collider",
    )


class ParadiseEntityProperties(PropertyGroup):
    """Authored Paradise data for one object. Mirrors ``EntityExport``."""

    is_entity: BoolProperty(  # type: ignore[valid-type]
        name="Paradise Entity",
        description="Export this object as a Paradise entity",
        default=False,
    )

    kind: EnumProperty(  # type: ignore[valid-type]
        name="Kind",
        description="Free-form entity label carried through to the contract",
        items=KIND_ITEMS,
        default="Prop",
    )
    custom_kind: StringProperty(  # type: ignore[valid-type]
        name="Custom Kind",
        description="Kind label used when Kind is set to Custom",
        default="",
    )

    active_on_load: BoolProperty(  # type: ignore[valid-type]
        name="Active On Load",
        description="Spawn this entity enabled",
        default=True,
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

    initial_animation: StringProperty(  # type: ignore[valid-type]
        name="Initial Animation",
        description="Animation clip to play on spawn",
        default="",
    )

    # -- Physics body -------------------------------------------------------------------

    is_dynamic_body: BoolProperty(  # type: ignore[valid-type]
        name="Dynamic Body",
        description="Simulate as a dynamic rigid body instead of static scenery",
        default=False,
    )
    body_mass: FloatProperty(name="Mass", default=1.0, min=0.0)  # type: ignore[valid-type]
    body_linear_damping: FloatProperty(  # type: ignore[valid-type]
        name="Linear Damping",
        description="Roll decay on a dynamic body",
        default=0.0,
        min=0.0,
    )
    body_restitution: FloatProperty(  # type: ignore[valid-type]
        name="Restitution",
        description=(
            "Bounce. On a dynamic body: body-to-body bounce. On a static body: the bounce "
            "dynamic bodies get off this surface"
        ),
        default=0.2,
        min=0.0,
        max=1.0,
    )
    body_friction: FloatProperty(name="Friction", default=0.5, min=0.0)  # type: ignore[valid-type]

    # -- Collider export ----------------------------------------------------------------

    physics_colliders: CollectionProperty(type=ColliderReference)  # type: ignore[valid-type]
    physics_colliders_index: IntProperty(default=0)  # type: ignore[valid-type]
    interaction_colliders: CollectionProperty(type=ColliderReference)  # type: ignore[valid-type]
    interaction_colliders_index: IntProperty(default=0)  # type: ignore[valid-type]

    # -- Sprite animation ---------------------------------------------------------------
    # Godot reads the sheet/grid/quad geometry off a Sprite3D child and takes only the clock
    # from the authoring node. Blender has no Sprite3D, so the whole component is authored
    # here and the exporter derives the quad size from the object's dimensions.

    sprite_enabled: BoolProperty(  # type: ignore[valid-type]
        name="Sprite Animation",
        description="Export a flipbook sprite-animation component",
        default=False,
    )
    sprite_sheet: StringProperty(  # type: ignore[valid-type]
        name="Sheet",
        description="Spritesheet image under the data directory's sprites folder",
        subtype="FILE_PATH",
        default="",
    )
    sprite_columns: IntProperty(name="Columns", default=1, min=1)  # type: ignore[valid-type]
    sprite_rows: IntProperty(name="Rows", default=1, min=1)  # type: ignore[valid-type]
    sprite_frame_count: IntProperty(  # type: ignore[valid-type]
        name="Frame Count",
        description="0 uses the full columns x rows grid",
        default=0,
        min=0,
    )
    sprite_fps: FloatProperty(name="FPS", default=10.0, min=0.0)  # type: ignore[valid-type]
    sprite_loop: BoolProperty(name="Loop", default=True)  # type: ignore[valid-type]
    sprite_billboard: BoolProperty(name="Billboard", default=True)  # type: ignore[valid-type]

    # -- Particle emitter ---------------------------------------------------------------

    particle_kind: EnumProperty(  # type: ignore[valid-type]
        name="Particles",
        items=PARTICLE_KIND_ITEMS,
        default="NONE",
        description=(
            "Emission is a cone around the object's +Y in contract space; "
            "particles live in world space"
        ),
    )
    particle_emit_rate: FloatProperty(name="Emit Rate", default=8.0, min=0.0)  # type: ignore[valid-type]
    particle_lifetime: FloatProperty(name="Lifetime", default=1.5, min=0.0)  # type: ignore[valid-type]
    particle_speed: FloatProperty(name="Speed", default=2.0, min=0.0)  # type: ignore[valid-type]
    particle_spread_degrees: FloatProperty(  # type: ignore[valid-type]
        name="Spread", default=25.0, min=0.0, max=180.0
    )
    particle_gravity: FloatProperty(name="Gravity", default=-9.8)  # type: ignore[valid-type]
    particle_drag: FloatProperty(name="Drag", default=0.0, min=0.0)  # type: ignore[valid-type]
    particle_start_size: FloatProperty(name="Start Size", default=0.25, min=0.0)  # type: ignore[valid-type]
    particle_end_size: FloatProperty(name="End Size", default=0.25, min=0.0)  # type: ignore[valid-type]
    particle_max_count: IntProperty(  # type: ignore[valid-type]
        name="Max Particles",
        description="Capped at 64 by the runtime's per-emitter snapshot buffer",
        default=64,
        min=1,
        max=64,
    )
    particle_seed: IntProperty(  # type: ignore[valid-type]
        name="Seed",
        description="Same seed, same particle stream in every host. 0 is replaced at export",
        default=1,
        min=0,
    )
    particle_color: FloatVectorProperty(  # type: ignore[valid-type]
        name="Color",
        subtype="COLOR",
        size=4,
        default=(1.0, 1.0, 1.0, 1.0),
        min=0.0,
        max=1.0,
    )
    particle_sheet: StringProperty(name="Sheet", subtype="FILE_PATH", default="")  # type: ignore[valid-type]
    particle_sheet_columns: IntProperty(name="Columns", default=1, min=1)  # type: ignore[valid-type]
    particle_sheet_rows: IntProperty(name="Rows", default=1, min=1)  # type: ignore[valid-type]
    particle_sheet_frame_count: IntProperty(name="Frame Count", default=0, min=0)  # type: ignore[valid-type]
    particle_sheet_fps: FloatProperty(  # type: ignore[valid-type]
        name="Sheet FPS",
        description="0 stretches the flipbook once over each particle's lifetime",
        default=0.0,
        min=0.0,
    )

    # -- Audio emitter ------------------------------------------------------------------
    # Event NAMES, not ids: the audio tool hashes the name when it generates banks, so storing a
    # resolved id here would pin the scene to one bank build. Nothing validates the name -- the
    # events live in the audio project, which Blender cannot see -- so a typo is a silent
    # emitter rather than an export error.

    audio_enabled: BoolProperty(  # type: ignore[valid-type]
        name="Audio Emitter",
        description="Export a positional sound source at this object's origin",
        default=False,
    )
    audio_start_event: StringProperty(  # type: ignore[valid-type]
        name="Start Event",
        description="Audio event to post, e.g. Play_Arcade_Bed. Not validated here",
        default="",
    )
    audio_stop_event: StringProperty(  # type: ignore[valid-type]
        name="Stop Event",
        description="Optional. Leave empty to stop the sound by its playing id instead",
        default="",
    )
    audio_play_on_start: BoolProperty(  # type: ignore[valid-type]
        name="Play On Start",
        description="Post the start event as soon as the scene loads (ambience, machinery)",
        default=True,
    )
    audio_is_3d: BoolProperty(  # type: ignore[valid-type]
        name="3D",
        description=(
            "Off makes the emitter 2D: placed here for authoring convenience, but heard at "
            "full level wherever the listener is. What music and narration want"
        ),
        default=True,
    )
    audio_attenuation_scale: FloatProperty(  # type: ignore[valid-type]
        name="Attenuation Scale",
        description="Scales the authored falloff distance, so one curve serves emitters of different size",
        default=1.0,
        min=0.01,
    )

    # -- Agent --------------------------------------------------------------------------

    is_agent: BoolProperty(  # type: ignore[valid-type]
        name="Agent",
        description="Export movement data; also excludes this object from the navmesh bake",
        default=False,
    )
    move_speed: FloatProperty(name="Move Speed", default=defaults.MOVE_SPEED, min=0.0)  # type: ignore[valid-type]
    acceleration: FloatProperty(name="Acceleration", default=defaults.ACCELERATION, min=0.0)  # type: ignore[valid-type]
    idle_animation: StringProperty(name="Idle Clip", default="")  # type: ignore[valid-type]
    walk_animation: StringProperty(name="Walk Clip", default="")  # type: ignore[valid-type]

    # -- Identity -----------------------------------------------------------------------

    entity_guid: StringProperty(  # type: ignore[valid-type]
        name="Entity GUID",
        description=(
            "Stable per-placement identity, minted on save. Empty until minted. "
            "Duplicating an object clears it so the copy gets its own"
        ),
        default="",
    )


# -- Resolved accessors ------------------------------------------------------------------
# The exporter reads through these rather than the raw properties so the fallback rules live
# in one place and match the Godot host's Resolved* members exactly.


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


def resolved_kind(props: ParadiseEntityProperties) -> str:
    if props.kind == "CUSTOM":
        return props.custom_kind.strip() or "Prop"
    return props.kind or "Prop"


def resolved_move_speed(props: ParadiseEntityProperties) -> float:
    return defaults.sanitize(props.move_speed, defaults.MOVE_SPEED)


def resolved_acceleration(props: ParadiseEntityProperties) -> float:
    return defaults.sanitize(props.acceleration, defaults.ACCELERATION)


def resolved_idle_animation(props: ParadiseEntityProperties) -> str:
    return props.idle_animation.strip() or defaults.IDLE_ANIMATION_FALLBACK


def resolved_walk_animation(props: ParadiseEntityProperties) -> str:
    return props.walk_animation.strip() or defaults.WALK_ANIMATION_FALLBACK


def resolved_initial_animation(props: ParadiseEntityProperties) -> str | None:
    """Empty means "no clip" -- exported as null, not as an empty string."""
    return props.initial_animation.strip() or None


#: Registered in order; ColliderReference must precede the group that points at it.
classes = (ColliderReference, ParadiseEntityProperties)


def register_pointers() -> None:
    Object.paradise = PointerProperty(type=ParadiseEntityProperties)


def unregister_pointers() -> None:
    if hasattr(Object, "paradise"):
        del Object.paradise
