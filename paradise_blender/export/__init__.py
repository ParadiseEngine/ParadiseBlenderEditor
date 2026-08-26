"""Scene export: Blender data -> the Paradise contract.

Mirrors ``ParadiseGodotEditor/addons/paradise/Export/``. :mod:`.scene` is the entry point;
everything else is a piece it dispatches to.

* :mod:`.scene`            -- the walk, and the document assembly
* :mod:`.entity`           -- one object -> its authored components
* :mod:`.transform`        -- convert-then-decompose (read this before touching transforms)
* :mod:`.collider`         -- collider objects -> shapes, with scale folding
* :mod:`.material`         -- Principled BSDF -> ``LevelMaterialData``
* :mod:`.mesh`             -- entity geometry -> GLB under ``data/Models/``
* :mod:`.light` :mod:`.world` -- the lamps and the environment, each its own object
* :mod:`.navmesh`          -- walkable geometry -> the .NET bridge -> DotRecast binary
* :mod:`.project_settings` -- ``data/ProjectSettings.json``
* :mod:`.ops`              -- operators and the save hook
"""

from __future__ import annotations

from . import (
    collider,
    entity,
    light,
    material,
    mesh,
    navmesh,
    ops,
    project_settings,
    scene,
    transform,
    world,
)

__all__ = [
    "collider",
    "entity",
    "light",
    "material",
    "mesh",
    "navmesh",
    "ops",
    "project_settings",
    "scene",
    "transform",
    "world",
]
