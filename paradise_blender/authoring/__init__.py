"""Authoring layer: how a Blender scene declares what to export. Blender marks an entity with
a flag on an ordinary object, not a node type (see :mod:`.entity`)."""

from __future__ import annotations

from . import authored_components, collider, entity, material_props, ops

__all__ = ["authored_components", "collider", "entity", "material_props", "ops"]
