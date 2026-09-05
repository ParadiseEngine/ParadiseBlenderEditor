"""Project files that can be picked as authored asset references.

A reference is ``{ guid, path }``. The GUID lives in the sidecar (``<file>.meta``); the path is
the assets-relative authoring path. This module only LISTS them -- writing a reference is the
panel's job, and verifying that guid and path name the same file is ``verify``'s.
"""

from __future__ import annotations

import os

from . import sidecar
from .asset_reference import AssetReference
from .project import MANIFEST_NAME, ProjectLayout

__all__ = ["list_assets", "read_sidecar_guid"]

_SKIP_DIRS = frozenset({".git", ".editor", "build", "bin", "obj"})


def list_assets(layout: ProjectLayout, kinds: list[str] | None = None) -> list[AssetReference]:
    """Every identified file under ``assets/`` whose extension is in *kinds*.

    *kinds* are extensions with the leading dot (``.toml``, ``.glb``). An empty/None list means
    every file that has a sidecar. ``project.toml`` is the layout marker, not an authored asset.
    """
    allowed = {kind.lower() if kind.startswith(".") else f".{kind.lower()}" for kind in kinds or ()}
    found: list[AssetReference] = []
    assets = layout.assets
    for root, dirs, files in os.walk(assets):
        dirs[:] = [name for name in dirs if name not in _SKIP_DIRS]
        for name in files:
            if name.endswith(sidecar.SUFFIX) or name == MANIFEST_NAME:
                continue
            ext = os.path.splitext(name)[1].lower()
            if allowed and ext not in allowed:
                continue
            absolute = os.path.join(root, name)
            guid = read_sidecar_guid(sidecar.path_for(absolute))
            if guid is None:
                continue
            found.append(AssetReference(guid, layout.relative(absolute)))
    found.sort(key=lambda item: item.path.lower())
    return found


def read_sidecar_guid(path: str) -> str | None:
    """The sidecar's ``guid`` in canonical spelling, or ``None`` when the file is missing,
    unreadable, or carries no identity."""
    meta = sidecar.read(path)
    return None if meta is None else meta.guid
