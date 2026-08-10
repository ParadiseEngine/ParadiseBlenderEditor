"""Authoring operators: mark entities and colliders, and find them again.

The "find them again" half matters more than it looks. Godot shows entity-ness in the
outliner because it is a node type; in Blender it is a hidden flag on an ordinary object, so
without :class:`PARADISE_OT_select_entities` an author has no way to see which objects in a
large scene are actually exported.
"""

from __future__ import annotations

import bpy
from bpy.types import Operator

from .. import log
from . import guid
from .collider import is_collider
from .entity import entity_objects, is_entity

__all__ = ["classes"]


class PARADISE_OT_make_entity(Operator):
    """Mark the selected objects as Paradise entities"""

    bl_idname = "paradise.make_entity"
    bl_label = "Make Paradise Entity"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context) -> bool:
        return bool(context.selected_objects)

    def execute(self, context):
        marked = 0
        for obj in context.selected_objects:
            if not obj.paradise.is_entity:
                obj.paradise.is_entity = True
                marked += 1
            # Mint immediately rather than waiting for save: the live-preview sync keys on
            # GUIDs, so an entity created during a preview session needs one right away.
            guid.ensure_entity_guid(obj)

        log.info(f"Marked {marked} object(s) as Paradise entities.", self)
        return {"FINISHED"}


class PARADISE_OT_clear_entity(Operator):
    """Stop exporting the selected objects as Paradise entities"""

    bl_idname = "paradise.clear_entity"
    bl_label = "Clear Paradise Entity"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context) -> bool:
        return any(is_entity(obj) for obj in context.selected_objects)

    def execute(self, context):
        cleared = 0
        for obj in context.selected_objects:
            if obj.paradise.is_entity:
                obj.paradise.is_entity = False
                cleared += 1
        # The GUID is deliberately left in place: re-marking the object later restores its
        # original identity instead of orphaning whatever referenced it.
        log.info(f"Cleared {cleared} object(s).", self)
        return {"FINISHED"}


class PARADISE_OT_make_collider(Operator):
    """Mark the selected objects as collider shapes"""

    bl_idname = "paradise.make_collider"
    bl_label = "Make Paradise Collider"
    bl_options = {"REGISTER", "UNDO"}

    shape: bpy.props.EnumProperty(  # type: ignore[valid-type]
        name="Shape",
        items=[("Box", "Box", ""), ("Sphere", "Sphere", ""), ("Capsule", "Capsule", "")],
        default="Box",
    )

    @classmethod
    def poll(cls, context) -> bool:
        return bool(context.selected_objects)

    def execute(self, context):
        for obj in context.selected_objects:
            obj.paradise_collider.is_collider = True
            obj.paradise_collider.shape = self.shape
        log.info(f"Marked {len(context.selected_objects)} collider(s) as {self.shape}.", self)
        return {"FINISHED"}


class PARADISE_OT_assign_colliders(Operator):
    """Assign the selected collider objects to the active entity"""

    bl_idname = "paradise.assign_colliders"
    bl_label = "Assign Colliders To Entity"
    bl_options = {"REGISTER", "UNDO"}

    slot: bpy.props.EnumProperty(  # type: ignore[valid-type]
        name="Slot",
        items=[
            ("PHYSICS", "Physics", "Solid collision shapes"),
            ("INTERACTION", "Interaction", "Interaction/sensor volumes"),
        ],
        default="PHYSICS",
    )

    @classmethod
    def poll(cls, context) -> bool:
        return bool(context.active_object and is_entity(context.active_object))

    def execute(self, context):
        entity = context.active_object
        collection = (
            entity.paradise.physics_colliders
            if self.slot == "PHYSICS"
            else entity.paradise.interaction_colliders
        )

        existing = {item.target for item in collection if item.target}
        added = 0
        skipped_non_colliders = 0

        for obj in context.selected_objects:
            if obj is entity or obj in existing:
                continue
            if not is_collider(obj):
                # Assigning an unmarked object would export a collider with no shape kind,
                # so require the mark rather than guessing Box.
                skipped_non_colliders += 1
                continue
            collection.add().target = obj
            added += 1

        if skipped_non_colliders:
            log.warn(
                f"{skipped_non_colliders} selected object(s) are not marked as Paradise colliders "
                "and were skipped. Use Make Paradise Collider first.",
                self,
            )
        log.info(f"Assigned {added} collider(s) to '{entity.name}'.", self)
        return {"FINISHED"}


class PARADISE_OT_remove_collider(Operator):
    """Remove a collider entry from the active entity"""

    bl_idname = "paradise.remove_collider"
    bl_label = "Remove Collider"
    bl_options = {"REGISTER", "UNDO"}

    slot: bpy.props.StringProperty(default="PHYSICS")  # type: ignore[valid-type]
    index: bpy.props.IntProperty(default=-1)  # type: ignore[valid-type]

    @classmethod
    def poll(cls, context) -> bool:
        return bool(context.active_object and is_entity(context.active_object))

    def execute(self, context):
        props = context.active_object.paradise
        collection = (
            props.physics_colliders if self.slot == "PHYSICS" else props.interaction_colliders
        )
        if 0 <= self.index < len(collection):
            collection.remove(self.index)
        return {"FINISHED"}


class PARADISE_OT_select_entities(Operator):
    """Select every Paradise entity in the scene"""

    bl_idname = "paradise.select_entities"
    bl_label = "Select Paradise Entities"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        bpy.ops.object.select_all(action="DESELECT")
        entities = entity_objects(context.scene)
        for obj in entities:
            # A hidden or unselectable object cannot be selected; skipping it silently is
            # better than raising, but it means the count reported is what was reachable.
            if obj.visible_get():
                obj.select_set(True)
        if entities:
            context.view_layer.objects.active = entities[0]
        log.info(f"{len(entities)} Paradise entity/entities in this scene.", self)
        return {"FINISHED"}


class PARADISE_OT_repair_guids(Operator):
    """Mint missing entity GUIDs and re-mint duplicates"""

    bl_idname = "paradise.repair_guids"
    bl_label = "Repair Entity GUIDs"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        changed = guid.ensure_unique_guids(context.scene)
        log.info(f"Repaired {changed} entity GUID(s).", self)
        return {"FINISHED"}


classes = (
    PARADISE_OT_make_entity,
    PARADISE_OT_clear_entity,
    PARADISE_OT_make_collider,
    PARADISE_OT_assign_colliders,
    PARADISE_OT_remove_collider,
    PARADISE_OT_select_entities,
    PARADISE_OT_repair_guids,
)
