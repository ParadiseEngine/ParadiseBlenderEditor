"""Authoring layer: how a Blender scene declares what Paradise should export.

Mirrors ``ParadiseGodotEditor/addons/paradise/Authoring/``. The central difference is that
Godot marks an entity by node type while Blender marks it with a flag on an ordinary object --
see :mod:`.entity` for why, and what that costs.

* :mod:`.entity`         -- the per-object entity property group (mirrors ``EntityExport``)
* :mod:`.collider`       -- collider shape marking and dimension resolution
* :mod:`.material_props` -- contract-only material parameters (recipes, transmission)
* :mod:`.guid`           -- stable per-placement identity, minted and de-duplicated on save
* :mod:`.ops`            -- operators for marking and finding entities
* :mod:`.defaults`       -- values shared with the Unity and Godot authoring layers
"""

from __future__ import annotations

from . import collider, defaults, entity, guid, material_props, ops

__all__ = ["collider", "defaults", "entity", "guid", "material_props", "ops"]
