"""Put objects under a new group, the way Unity's "Create Empty Parent" does.

A group is an ordinary document object -- meta and transform, nothing else -- shown as an Empty
its members are parented to (see ``save._adopt_new_groups`` for the one an author makes by hand).
This is the one-gesture version: an Empty at the active object's place, hung where the members
hung, with each member re-parented so it does not move.

Parenting is done with an IDENTITY ``matrix_parent_inverse`` and the local channels rewritten,
never through Blender's ``parent_set``: that keeps the object in place by storing the offset in
the inverse, which the document has no field for. ``save`` folds a stray inverse in for objects
parented by hand; this module never creates one.
"""

from __future__ import annotations

import uuid

import bpy
from mathutils import Matrix

from . import store

__all__ = ["GroupError", "group_objects", "parent_keeping_world"]


class GroupError(Exception):
    """Why the group cannot be made; the operator shows it."""


def group_objects(scene: bpy.types.Scene, members: list, active=None) -> bpy.types.Object:
    """A new group holding ``members``, returned so the caller can select it.

    The group hangs where the ACTIVE member hung (else the first), and sits at that member's
    world position with no rotation or scale, so it is a sensible pivot to move the group by.
    Members that were nested inside another member stay where they are: the group holds the
    topmost ones and the rest come along.
    """
    members = [obj for obj in members if obj is not None]
    if not members:
        raise GroupError("Select at least one document object to group.")
    for obj in members:
        if store.guid_of(obj) is None:
            raise GroupError(f"'{obj.name}' is not a document object.")
        if store.is_derived(obj):
            raise GroupError(
                f"'{obj.name}' belongs to a prefab instance; group the instance instead.")
        if obj.parent is None:
            raise GroupError(f"'{obj.name}' is the document root and cannot be grouped.")

    anchor = active if active in members else members[0]
    bpy.context.view_layer.update()

    group = bpy.data.objects.new("Group", None)
    group.empty_display_type = "PLAIN_AXES"
    group.empty_display_size = 0.5
    for collection in anchor.users_collection:
        collection.objects.link(group)
    if not group.users_collection:
        scene.collection.objects.link(group)
    store.tag_object(group, str(uuid.uuid4()), [])
    store.tag_name(group, group.name)

    parent_keeping_world(group, anchor.parent, Matrix.Translation(anchor.matrix_world.translation))

    chosen = set(members)
    for obj in members:
        if any(ancestor in chosen for ancestor in _ancestors(obj)):
            continue
        parent_keeping_world(obj, group)
    return group


def parent_keeping_world(obj: bpy.types.Object, parent, world: Matrix | None = None) -> None:
    """Parent ``obj`` under ``parent`` with an identity inverse, staying at ``world`` (its
    current placement when not given). The local channels are what the save writes, so they
    are what gets rewritten."""
    if world is None:
        bpy.context.view_layer.update()
        world = obj.matrix_world.copy()
    obj.parent = parent
    obj.matrix_parent_inverse.identity()
    obj.matrix_basis = world if parent is None else parent.matrix_world.inverted() @ world


def _ancestors(obj):
    current = obj.parent
    while current is not None:
        yield current
        current = current.parent
