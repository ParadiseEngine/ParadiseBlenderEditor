"""Delete exported artifacts that nothing references any more.

An export writes what the scene needs; nothing ever removed what it stopped needing. Rename a
mesh datablock and its old ``Models/<old>.glb`` and every ``<old>.<image>.ktx2`` sidecar stay
forever -- in ShiningPie's case inside a committed, LFS-tracked ``data/``, where they are dead
weight in every clone and a red herring in every "which file does the game actually load?"
question. The measured backlog there was small (3 files) precisely because the directory is
young; the point is that it only ever grows.

**The rule is reachability, not bookkeeping.** The scene documents under ``data/scenes`` are
roots, and an artifact is live if some root can reach it. Bookkeeping -- remembering what the
last export wrote -- would be wrong the first time a second .blend exported into the same
``data/``, or the first time someone cloned the repo without the record.

Three properties make this safe enough to run on every export, and all three are load-bearing:

* **Only inside owned directories, only owned extensions** (:data:`OWNED`). Wwise's banks under
  ``data/audio``, the game's own ``data/shiningpie/config.json``, an author's stray note in
  ``Models/`` -- none are ours, so none are candidates. The exporter deletes only the kinds of
  file it writes, in the places it writes them.
* **Every scene document is a root, not just the one being exported.** A ``data/`` shared by two
  .blends must not have one export delete the other's meshes.
* **Reachability is computed from string VALUES, not from known keys.** Any string in any root
  that resolves to a file under ``data/`` marks it live. A key whitelist would go stale the day a
  component gains a new asset field, and the failure mode of going stale here is deleting a live
  asset -- so the fuzzy rule is the correct one. It errs toward keeping.

Pruning is cheap to get wrong in one direction and expensive in the other, so everything
ambiguous keeps the file. It is also cheap to undo: ``data/`` is committed, and a deleted KTX2
comes back from :mod:`.cache` on the next export without re-encoding.
"""

from __future__ import annotations

import json
import os

from .. import log
from ..paths import ExportPaths
from .glb_textures import external_image_uris

__all__ = ["OWNED", "prune_orphans"]

#: Directories this exporter owns, mapped to the file extensions it writes there. Anything else
#: under ``data/`` belongs to another tool and is never a deletion candidate.
#:
#: ``scenes`` lists ``.bin`` only: the navmesh binaries are ours, but the scene documents beside
#: them are the ROOTS of the whole sweep and are never deleted -- deleting a root would make
#: everything it referenced garbage on the next pass, which is how a cleanup turns into a wipe.
OWNED: dict[str, tuple[str, ...]] = {
    "Models": (".glb", ".ktx2"),
    "primitives": (".glb", ".ktx2"),
    "materials": (".json",),
    "prefabs": (".json",),
    "sprites": (".ktx2",),
    "scenes": (".bin",),
}

#: Keys under which a prefab document states its own identity. A scene entity references a prefab
#: by asset path and guid rather than by document path, so this is the one place a filename-based
#: rule cannot work and identity has to be matched instead.
PREFAB_IDENTITY_KEYS = ("PrefabAssetPath", "PrefabGuid", "DisplayName")


def prune_orphans(paths: ExportPaths, dry_run: bool = False) -> list[str]:
    """Remove unreferenced artifacts under ``data/``. Returns the data-relative paths removed.

    Returns an empty list, having warned, whenever the sweep cannot be trusted: an unreadable
    scene document, or roots that collectively declare no entities at all. The second case is not
    hypothetical caution -- an export that found nothing (wrong .blend open, a scene whose objects
    were never marked as entities) writes a valid, empty document, and a reachability sweep
    against it would find the entire directory unreachable.
    """
    scene_documents = _scene_documents(paths)
    if scene_documents is None:
        return []

    if not any(document.get("Entities") for _path, document in scene_documents):
        log.warn(
            "Skipping the data-directory cleanup: no exported scene declares any entities, so "
            "everything under data/ would look unreferenced. Export a scene with entities first."
        )
        return []

    # Normalized field -> the field as actually spelled on disk. Two forms because they answer
    # two different questions: comparison must be case-insensitive where the filesystem is (or a
    # reference to `models/x.glb` orphans `Models/x.glb`), while OPENING a file must use the real
    # spelling (or the same folding breaks every read on a case-sensitive filesystem, and a GLB
    # that cannot be read is a GLB whose textures look unreferenced).
    live: dict[str, str] = {}
    strings: set[str] = set()

    seeds = list(scene_documents)
    settings = paths.project_settings_output_path()
    settings_document = _read_json(settings)
    if settings_document is not None:
        seeds.append((settings, settings_document))

    _walk_documents(paths, seeds, live, strings)
    _collect_sidecars(paths, live)

    removed: list[str] = []
    for field in _owned_files(paths):
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
    """Every scene document, or ``None`` if any of them could not be read.

    All-or-nothing on purpose: a sweep run against a partial view of the roots would delete the
    assets belonging to the document it failed to parse.
    """
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
    strings: set[str],
) -> None:
    """Mark everything reachable from the root documents, following documents transitively.

    Transitively, because references chain: a scene names a material document, and that document
    names the texture it samples. Marking only what the scenes name directly would have deleted
    those textures -- caught by a test, not by review, which is the argument for the fixpoint over
    a hand-enumerated two-level walk.

    Prefab templates join the frontier by IDENTITY rather than by path. They are the one artifact
    a scene does not address by filename: an entity carries the prefab's asset path and guid,
    while the document is named after the collection. A template whose identity appears in no
    scene is genuinely orphaned -- a prefab collection that was renamed or deleted.
    """
    queue = list(seeds)
    walked: set[str] = set()

    while queue:
        path, document = queue.pop()
        key = _normalized(os.path.abspath(path))
        if key in walked:
            continue
        walked.add(key)

        _collect(document, os.path.dirname(path), paths, live, strings)
        _mark_matching_prefabs(paths, live, strings)

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


def _mark_matching_prefabs(paths: ExportPaths, live: dict[str, str], strings: set[str]) -> None:
    """Mark prefab templates whose declared identity some document names."""
    directory = os.path.join(paths.data_dir, "prefabs")
    if not os.path.isdir(directory):
        return

    for name in sorted(os.listdir(directory)):
        field = f"prefabs/{name}"
        if not name.lower().endswith(".json") or _normalized(field) in live:
            continue

        document = _read_json(os.path.join(directory, name))
        if document is None:
            # Unreadable is not evidence of being unreferenced. Keep it.
            live[_normalized(field)] = field
            continue

        identities = {document.get(key) for key in PREFAB_IDENTITY_KEYS} - {None}
        if identities & strings:
            live[_normalized(field)] = field


def _collect(
    node: object, directory: str, paths: ExportPaths, live: dict[str, str], strings: set[str]
) -> None:
    """Walk a document, marking every string that names a real file under ``data/``.

    Strings are resolved both as data-relative fields (how the contract addresses assets) and as
    siblings of the document holding them (how ``NavMeshFile`` is defined). Marking on existence
    rather than on syntax means a coincidental match keeps a file alive, which is the harmless
    direction.
    """
    if isinstance(node, dict):
        for value in node.values():
            _collect(value, directory, paths, live, strings)
    elif isinstance(node, list):
        for item in node:
            _collect(item, directory, paths, live, strings)
    elif isinstance(node, str) and node:
        strings.add(node)
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
    """Mark the KTX2 sidecars that live GLBs point at.

    The only reference that is not in a JSON document: a mesh's textures are named by image URIs
    inside the GLB itself. Reading them is also what makes a sidecar orphaned by an edit -- a
    material that lost its texture map -- collectable at all.
    """
    for field in [f for f in list(live.values()) if f.lower().endswith(".glb")]:
        glb_path = paths.output_path_for_field(field)
        for uri in external_image_uris(glb_path):
            sidecar = _as_existing_field(os.path.join(os.path.dirname(field), uri), paths)
            if sidecar is not None:
                live[_normalized(sidecar)] = sidecar


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
    """Case-folded on case-insensitive filesystems, so a reference spelled ``models/x.glb`` and a
    file at ``Models/x.glb`` are one entry rather than an orphan plus a dangling link."""
    return os.path.normcase(field.replace("\\", "/"))
