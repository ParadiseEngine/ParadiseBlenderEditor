"""Creating a new ``*.prefab``: write the document, then wait for its identity.

The addon writes documents; ``paradise assets watch`` writes identities (:mod:`sidecar`). So a
creation is not finished when the file lands -- it is finished when the watcher has minted the
sidecar, because until then there is no guid for anything to reference the new prefab BY. A
caller that needs the reference (an extraction, which must write it into the document it took
the subtree out of) therefore has to wait, and a creation with no watcher running cannot
complete at all.
"""

from __future__ import annotations

import os
import time
import uuid

from . import atomic, sidecar, well_known
from . import guid as document_guid
from . import prefab as prefab_document
from .asset_reference import AssetReference
from .prefab import PrefabComponent, PrefabDocument, PrefabObject
from .project import ProjectLayout

__all__ = [
    "IDENTITY_TRANSFORM",
    "CreateError",
    "create",
    "identify",
    "identify_all",
    "refuse_target",
    "root_only",
    "write",
]

#: A transform with no rotation, at the origin, unscaled -- what a prefab's root carries, since
#: an instance places the prefab and a root offset would fight it.
IDENTITY_TRANSFORM = {
    well_known.POSITION: [0.0, 0.0, 0.0],
    well_known.ROTATION: [0.0, 0.0, 0.0, 1.0],
    well_known.SCALE: [1.0, 1.0, 1.0],
}


class CreateError(Exception):
    """A prefab could not be created. The message is for the author."""


def root_only(name: str, guid: str | None = None, meta: dict | None = None) -> PrefabDocument:
    """A document holding one object: a named identity at the origin.

    The guid here is an OBJECT identity, local to this document -- not the asset identity in the
    sidecar, which only the watcher mints.
    """
    data: dict = {
        well_known.GUID: document_guid.canonical(guid) if guid else str(uuid.uuid4()),
        well_known.NAME: name,
    }
    data.update(meta or {})
    root = PrefabObject(components=[
        PrefabComponent(well_known.META_ID, well_known.META_TYPE, data),
        PrefabComponent(well_known.TRANSFORM_ID, well_known.TRANSFORM_TYPE, dict(IDENTITY_TRANSFORM)),
    ])
    return PrefabDocument(objects=[root])


def refuse_target(path: str, layout: ProjectLayout) -> str:
    """The assets-relative path a new prefab at ``path`` would have, or raise. Separate from
    :func:`create` so an operator can refuse before it starts saving the open scene."""
    absolute = os.path.abspath(path)
    relative = os.path.relpath(absolute, os.path.abspath(layout.assets))
    if relative.startswith(os.pardir) or os.path.isabs(relative):
        raise CreateError(
            f"{absolute} is outside {layout.assets}. A prefab the project cannot reference by "
            "an assets-relative path is a prefab nothing can instantiate."
        )
    if os.path.exists(absolute):
        raise CreateError(f"{relative} already exists. Pick another name, or delete it first.")
    if os.path.exists(sidecar.path_for(absolute)):
        raise CreateError(
            f"{relative}{sidecar.SUFFIX} exists without its document. Delete the stray sidecar, "
            "or restore the document it identifies -- a new file there would take over its identity."
        )
    return relative.replace(os.sep, "/")


def write(path: str, layout: ProjectLayout, document: PrefabDocument) -> str:
    """Write ``document`` as a new prefab at ``path`` and return its assets-relative path.

    The file now exists and is valid; it has no IDENTITY until the watcher mints one, so a
    caller that needs to reference it must go on to :func:`identify`.
    """
    relative = refuse_target(path, layout)
    absolute = os.path.abspath(path)
    document.validate(relative)

    os.makedirs(os.path.dirname(absolute) or ".", exist_ok=True)
    atomic.write_text(absolute, prefab_document.dumps(document))
    return relative


def identify(
    path: str, relative: str, timeout: float = sidecar.WAIT_SECONDS
) -> AssetReference:
    """Wait for the watcher's identity for ``path``, or raise :class:`CreateError`.

    The file is left in place on a timeout: it is valid content, and the watcher will identify
    it the moment one runs -- deleting the author's new prefab because a background process was
    not started would be the worse failure.
    """
    identified = sidecar.wait_for(os.path.abspath(path), timeout)
    if identified is None:
        raise CreateError(
            f"{relative} was written, but no {sidecar.SUFFIX} appeared for it within "
            f"{timeout:.0f}s, so it has no identity to be referenced by. The asset watcher is "
            "what mints one (Paradise Assets > Play > Start)."
        )
    return AssetReference(identified.guid, relative)


def identify_all(
    written: dict[str, str], timeout: float = sidecar.WAIT_SECONDS
) -> tuple[dict[str, AssetReference], list[str]]:
    """Identify a batch of freshly written prefabs under ONE deadline, and say which are still
    unidentified when it passes.

    One deadline rather than one each: the watcher reconciles a batch of new files together, so
    waiting per file turns a single reconcile into N timeouts -- which is how generating a
    project's worth of model prefabs stalled after the first two.
    """
    deadline = time.monotonic() + timeout
    found: dict[str, AssetReference] = {}
    missing: list[str] = []
    for absolute, relative in written.items():
        remaining = max(0.0, deadline - time.monotonic())
        identified = sidecar.wait_for(absolute, remaining)
        if identified is None:
            missing.append(relative)
            continue
        found[absolute] = AssetReference(identified.guid, relative)
    return found, missing


def create(
    path: str,
    layout: ProjectLayout,
    document: PrefabDocument,
    timeout: float = sidecar.WAIT_SECONDS,
) -> AssetReference:
    """Write a new prefab and return the reference the watcher minted for it."""
    relative = write(path, layout, document)
    return identify(path, relative, timeout)
