"""Delete exported artifacts that no scene document references any more.

Reachability, not bookkeeping: a record of what the last export wrote is wrong the first time
a second .blend exports into the same ``data/`` or someone clones without the record. Three
rules keep it safe, and each is load-bearing: only owned directories and extensions
(:data:`OWNED`) are candidates, so Wwise banks and the game's config are never touched; EVERY
scene document is a root, so two .blends sharing a ``data/`` cannot delete each other's meshes;
reachability is computed from string VALUES rather than known keys, because a key whitelist
goes stale the day a component gains an asset field and the cost of stale is deleting a live
asset. Ambiguity keeps the file. Off by default; the only destructive step in an export.

KNOWN GAP (#28): since v5 nothing in the document names ``scenes/<scene>.navmesh.bin``, so
with pruning on every export deletes the navmesh it just baked.
"""

from __future__ import annotations

import contextlib
import json
import os

from .. import log
from ..paths import ExportPaths
from .glb_textures import external_image_uris
from .ktx import SOURCE_EXTENSIONS

__all__ = ["OWNED", "prune_orphans"]

#: Directories this exporter writes, and the extensions it writes there. ``scenes`` lists
#: ``.bin`` only: the documents are the ROOTS and deleting one turns the next pass into a wipe.
#: ``primitives/`` is absent on purpose: the Godot host generates those, and owning what you
#: cannot regenerate is how a cleanup loses an asset.
OWNED: dict[str, tuple[str, ...]] = {
    "Models": (".glb", ".ktx2"),
    "materials": (".json",),
    "sprites": (".ktx2",),
    "scenes": (".bin",),
}


def prune_orphans(paths: ExportPaths, dry_run: bool = False) -> list[str]:
    """Remove unreferenced artifacts; returns the data-relative paths removed. Refuses (empty
    list, warned) on an unreadable root or when no root declares an entity: an export that found
    nothing writes a valid empty document, against which the whole directory is unreachable."""
    scene_documents = _scene_documents(paths)
    if scene_documents is None:
        return []

    if not any(document.get("Entities") for _path, document in scene_documents):
        log.warn(
            "Skipping the data-directory cleanup: no exported scene declares any entities, so "
            "everything under data/ would look unreferenced. Export a scene with entities first."
        )
        return []

    # Normalized -> on-disk spelling: comparison must fold case where the filesystem does, but
    # opening must use the real spelling or every read fails on a case-sensitive one.
    live: dict[str, str] = {}

    seeds = list(scene_documents)
    settings = paths.project_settings_output_path()
    settings_document = _read_json(settings)
    if settings_document is not None:
        seeds.append((settings, settings_document))

    owned = _owned_files(paths)
    _walk_documents(paths, seeds, live)
    _collect_sidecars(paths, live)
    _collect_transcode_targets(paths, owned, live)

    removed: list[str] = []
    for field in owned:
        if _normalized(field) in live:
            continue
        removed.append(field)
        if not dry_run:
            try:
                os.unlink(paths.output_path_for_field(field))
            except OSError as error:
                log.warn(f"Could not delete unreferenced '{field}': {error}")
                removed.pop()

    return sorted(removed)


def _scene_documents(paths: ExportPaths) -> list[tuple[str, dict]] | None:
    """Every scene document, or ``None`` if any failed: a partial view would delete the
    unreadable document's assets."""
    if not os.path.isdir(paths.scenes_dir):
        return []

    documents: list[tuple[str, dict]] = []
    for name in sorted(os.listdir(paths.scenes_dir)):
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(paths.scenes_dir, name)
        document = _read_json(path)
        if document is None:
            log.warn(
                f"Skipping the data-directory cleanup: '{name}' could not be read, and pruning "
                "without knowing what it references would delete assets that are still in use."
            )
            return None
        documents.append((path, document))
    return documents


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _walk_documents(
    paths: ExportPaths,
    seeds: list[tuple[str, dict]],
    live: dict[str, str],
) -> None:
    """Mark everything reachable from the roots, following documents transitively (scene ->
    material -> texture); a two-level walk deleted the textures, caught by a test."""
    queue = list(seeds)
    walked: set[str] = set()

    while queue:
        path, document = queue.pop()
        key = _normalized(os.path.abspath(path))
        if key in walked:
            continue
        walked.add(key)

        _collect(document, os.path.dirname(path), paths, live)

        for field in sorted(live.values()):
            if not field.lower().endswith(".json"):
                continue
            document_path = paths.output_path_for_field(field)
            if _normalized(os.path.abspath(document_path)) in walked:
                continue
            found = _read_json(document_path)
            if found is None:
                # Unreadable, and already marked live: keep it, and do not retry forever.
                walked.add(_normalized(os.path.abspath(document_path)))
                continue
            queue.append((document_path, found))


def _collect(
    node: object, directory: str, paths: ExportPaths, live: dict[str, str]
) -> None:
    """Mark every string that names a real file, data-relative or sibling; a coincidental match
    keeps a file, the harmless direction."""
    if isinstance(node, dict):
        for value in node.values():
            _collect(value, directory, paths, live)
    elif isinstance(node, list):
        for item in node:
            _collect(item, directory, paths, live)
    elif isinstance(node, str) and node:
        for candidate in (node, os.path.join(directory, node)):
            field = _as_existing_field(candidate, paths)
            if field is not None:
                live[_normalized(field)] = field


def _as_existing_field(reference: str, paths: ExportPaths) -> str | None:
    """The normalized data-relative field for ``reference``, if it names a file under ``data/``."""
    normalized = reference.replace("\\", "/").strip()
    if not normalized or normalized.startswith("//"):
        return None

    absolute = normalized if os.path.isabs(normalized) else os.path.join(paths.data_dir, normalized)
    field = paths.data_relative_field(os.path.normpath(absolute))
    if field is None:
        return None
    return field if os.path.isfile(paths.output_path_for_field(field)) else None


def _collect_sidecars(paths: ExportPaths, live: dict[str, str]) -> None:
    """Mark the KTX2 sidecars live GLBs point at, the one reference not in a JSON document."""
    for field in [f for f in list(live.values()) if f.lower().endswith(".glb")]:
        glb_path = paths.output_path_for_field(field)
        for uri in external_image_uris(glb_path):
            sidecar = _as_existing_field(os.path.join(os.path.dirname(field), uri), paths)
            if sidecar is not None:
                live[_normalized(sidecar)] = sidecar


def _collect_transcode_targets(
    paths: ExportPaths, owned: list[str], live: dict[str, str]
) -> None:
    """Keep every ``.ktx2`` with a source image beside it: ``convert_data_directory`` transcodes
    in place and the source is the author's, so deleting one half of the pair cleans nothing and
    would eat a spritesheet in the window between transcoding it and wiring it to an entity."""
    candidates = [f for f in owned if f.lower().endswith(".ktx2")]
    if not candidates:
        return

    # Listing, not probing `stem + ".png"`: `Fire.PNG` must still shield `Fire.ktx2`.
    sources: set[str] = set()
    for directory in {os.path.dirname(field) for field in candidates}:
        absolute = os.path.join(paths.data_dir, directory) if directory else paths.data_dir
        with contextlib.suppress(OSError):
            for name in os.listdir(absolute):
                stem, extension = os.path.splitext(name)
                if extension.lower() in SOURCE_EXTENSIONS:
                    sources.add(_normalized(os.path.join(directory, stem)))

    for field in candidates:
        if _normalized(field) in live:
            continue
        if _normalized(os.path.splitext(field)[0]) in sources:
            live[_normalized(field)] = field


def _owned_files(paths: ExportPaths) -> list[str]:
    """Every data-relative file that this exporter could have written, in a stable order."""
    found: list[str] = []
    for subdirectory, extensions in sorted(OWNED.items()):
        root = os.path.join(paths.data_dir, subdirectory)
        for directory, _dirs, files in os.walk(root):
            for name in sorted(files):
                if name.startswith(".") or not name.lower().endswith(extensions):
                    continue
                full = os.path.join(directory, name)
                found.append(os.path.relpath(full, paths.data_dir).replace(os.sep, "/"))
    return found


def _normalized(field: str) -> str:
    """Case-folded where the filesystem is, or ``models/x.glb`` orphans ``Models/x.glb``."""
    return os.path.normcase(field.replace("\\", "/"))
