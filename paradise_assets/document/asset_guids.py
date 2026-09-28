"""The identity of each model of a multi-asset ``.blend``: a GUID stored IN the ``.blend``.

An asset-marked collection is one model (``model_source``), and its identity is the collection's
``paradise_guid`` custom property -- a canonical, lowercase hyphenated GUID -- never its name, so
renaming the collection keeps every document, clip setting and placement that names it. The
engine never writes a ``.blend``: the GUIDs are minted by this addon when a ``.blend`` holding
asset collections is saved (``materialize/asset_guids.py``) and by ``paradise assets to-blend``.
The converter refuses a file where an asset collection has none, or two share one.

Blender copies custom properties when a collection is duplicated, so a copy arrives holding its
original's GUID. :func:`assignments` settles that on save: the collection the project already
knows under that GUID keeps it, and the copy gets a fresh one.

Imports no ``bpy``.
"""

from __future__ import annotations

import tomllib
import uuid
from collections.abc import Callable, Iterable, Mapping

from . import guid as document_guid
from . import mesh_document, project, sidecar

__all__ = ["PROPERTY", "assignments", "canonical_of", "recorded_names"]

#: The collection custom property holding an asset collection's GUID.
PROPERTY = "paradise_guid"

#: The ``[extract]`` sidecar domain the engine records each extracted part in, and the part keys
#: read here (``ExtractionRecord`` in Paradise.Assets.Pipeline).
_EXTRACT = "extract"
_PARTS = "parts"
_PART_ASSET = "asset"
_PART_NAME = "name"
_PART_PATH = "path"


def canonical_of(value: object) -> str | None:
    """``value`` as a canonical asset GUID, or ``None`` when it is not a usable one: absent, not
    text, not a GUID, or the all-zero GUID."""
    return document_guid.canonical(value) if document_guid.is_text(value) else None


def assignments(
    collections: Iterable[tuple[str, object]],
    recorded: Callable[[], Mapping[str, Iterable[str]]],
    mint: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> dict[str, str]:
    """The ``paradise_guid`` to write per asset collection, by name, for ``collections``
    (``(name, current property value)`` pairs, names unique as Blender keeps them); a collection
    whose value is already right is absent.

    A collection without a valid GUID gets a fresh one; a valid one in another spelling is
    rewritten canonical. Where several share a GUID, the one the project lists under it keeps it
    (``recorded()``: canonical GUID -> the names the project knows it by, read only when there is
    such a conflict), else the first by name, and every other gets a fresh one."""
    by_guid: dict[str, list[str]] = {}
    changes: dict[str, str] = {}
    for name, value in sorted(collections, key=lambda item: item[0]):
        guid = canonical_of(value)
        if guid is None:
            changes[name] = mint()
            continue
        by_guid.setdefault(guid, []).append(name)
        if value != guid:
            changes[name] = guid

    shared = {guid: names for guid, names in by_guid.items() if len(names) > 1}
    known_names = recorded() if shared else {}
    for guid, names in shared.items():
        known = set(known_names.get(guid, ()))
        keeper = next((name for name in names if name in known), names[0])
        for name in names:
            if name != keeper:
                changes[name] = mint()
    return changes


def recorded_names(blend: str) -> dict[str, set[str]]:
    """What the project records about the asset collections of the ``.blend`` at ``blend``:
    canonical GUID -> the names it knows the asset by -- each extracted part's ``name`` in the
    sidecar's ``[extract]`` record and the name hint of each extracted mesh document. Empty
    outside a project, and for a file never extracted."""
    layout = project.locate(blend)
    if layout is None:
        return {}
    try:
        with open(sidecar.path_for(blend), "rb") as handle:
            root = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    domain = root.get(_EXTRACT)
    parts = domain.get(_PARTS) if isinstance(domain, dict) else None
    found: dict[str, set[str]] = {}
    for part in parts if isinstance(parts, list) else []:
        guid = canonical_of(part.get(_PART_ASSET)) if isinstance(part, dict) else None
        if guid is None:
            continue
        names = found.setdefault(guid, set())
        name = part.get(_PART_NAME)
        if isinstance(name, str):
            names.add(name)
        path = part.get(_PART_PATH)
        if isinstance(path, str) and mesh_document.is_document(path):
            model = mesh_document.source_for(layout, path)
            if model is not None and model.asset == guid and model.name:
                names.add(model.name)
    return found
