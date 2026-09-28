"""Give each local asset collection of the ``.blend`` being saved its Paradise GUID
(``document/asset_guids.py``), on every save while the addon is enabled -- a model file opened by
Edit Source in New Blender as much as a level's working file."""

from __future__ import annotations

import bpy

from ..document import asset_guids

__all__ = ["stamp"]


def stamp(file_path: str) -> dict[str, str]:
    """Write a fresh or canonical ``paradise_guid`` onto each local asset-marked collection that
    needs one; the GUID written per collection name. ``file_path`` is where the ``.blend`` is
    being saved: its sidecar settles a GUID two collections share."""
    collections = [
        collection for collection in bpy.data.collections
        if collection.library is None and collection.asset_data is not None
    ]
    if not collections:
        return {}
    path = file_path or bpy.data.filepath
    changes = asset_guids.assignments(
        [(collection.name, collection.get(asset_guids.PROPERTY)) for collection in collections],
        lambda: asset_guids.recorded_names(path) if path else {},
    )
    for collection in collections:
        if collection.name in changes:
            collection[asset_guids.PROPERTY] = changes[collection.name]
    return changes
