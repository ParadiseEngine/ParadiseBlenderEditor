"""Following a ``.mesh`` / ``.skinnedmesh`` document back to the GLB it names.

A prefab references the mesh DOCUMENT (a GLB ships nothing, and the build refuses a reference to
one), but Blender can only import the GLB. The document is a small TOML the engine's extractor
writes -- ``source = { guid, path }`` naming the GLB, assets-relative -- so the viewport reads that
one field and imports what it points at. Nothing else in the document is interpreted here.
"""

from __future__ import annotations

import tomllib

from .project import ProjectLayout

__all__ = ["SUFFIXES", "displayable", "glb_for", "is_document"]

#: The geometry documents the build cooks to a mesh blob; a rigged model's is its own kind.
SUFFIXES = (".mesh", ".skinnedmesh")


def is_document(path: str) -> bool:
    return path.lower().endswith(SUFFIXES)


def glb_for(layout: ProjectLayout, path: str) -> str | None:
    """The absolute GLB path a mesh document at assets-relative ``path`` names, or ``None`` when
    the document is missing, unreadable, or names nothing. Unreadable reads as absent on purpose:
    the caller leaves the object an empty with a warning, which is what a placement whose mesh
    cannot be shown already does."""
    absolute = layout.resolve(path)
    try:
        with open(absolute, "rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    source = document.get("source")
    glb = source.get("path") if isinstance(source, dict) else None
    if not isinstance(glb, str) or not glb:
        return None
    return layout.resolve(glb)


def displayable(layout: ProjectLayout, path: str) -> str | None:
    """What to import for a mesh field: the GLB itself, or the GLB a document names."""
    if is_document(path):
        return glb_for(layout, path)
    return layout.resolve(path)
