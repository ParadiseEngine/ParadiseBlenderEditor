"""Following a ``.mesh`` / ``.skinnedmesh`` document back to the model it was extracted from.

A prefab references the mesh DOCUMENT (a model ships nothing, and the build refuses a reference
to one), but Blender can only show the model. The document is a small TOML the engine's extractor
writes -- ``source = { guid, path }`` naming the model (a ``.glb``, a ``.gltf`` or a converted
source), assets-relative, and beside it ``asset = "<name>"`` when the model is one asset of a
``.blend`` that holds several -- so the viewport reads those two fields and shows what they point
at (``model_source`` says how that source is loaded). Nothing else in the document is interpreted
here.
"""

from __future__ import annotations

import tomllib

from .model_source import Model, is_asset_name
from .project import ProjectLayout

__all__ = ["ASSET_KEY", "SUFFIXES", "displayable", "is_document", "source_for"]

#: The geometry documents the build cooks to a mesh blob; a rigged model's is its own kind.
SUFFIXES = (".mesh", ".skinnedmesh")

#: The top-level key naming the asset of a multi-asset source; absent for a whole-file model.
ASSET_KEY = "asset"


def is_document(path: str) -> bool:
    return path.lower().endswith(SUFFIXES)


def source_for(layout: ProjectLayout, path: str) -> Model | None:
    """The model a mesh document at assets-relative ``path`` names, or ``None`` when the document
    is missing, unreadable, or names nothing -- or an asset no file could be named after.
    Unreadable reads as absent on purpose: the caller leaves the object an empty with a warning,
    which is what a placement whose mesh cannot be shown already does."""
    absolute = layout.resolve(path)
    try:
        with open(absolute, "rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    source = document.get("source")
    model = source.get("path") if isinstance(source, dict) else None
    if not isinstance(model, str) or not model:
        return None
    asset = document.get(ASSET_KEY)
    if asset is not None and not is_asset_name(asset):
        return None
    return Model(layout.resolve(model), asset)


def displayable(layout: ProjectLayout, path: str) -> Model | None:
    """The model to show for a mesh field: the model itself, or the model a document names."""
    if is_document(path):
        return source_for(layout, path)
    return Model(layout.resolve(path))
