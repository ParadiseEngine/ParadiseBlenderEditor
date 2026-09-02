"""The disposable ``.editor/blend/<path>.blend`` a document is worked on in. It keeps what a
document has no place for (camera, selection, extras) and is trusted only while its recorded
``(mtime, size)`` stamp matches; opening it still rematerializes the objects from ``assets/``
so edits from other tools do not sit stale. The session ADOPTS its path, so Ctrl+S updates the
workfile while the document is written by the sync handler.
"""

from __future__ import annotations

import os

import bpy

from ..document import project
from . import store, sync

__all__ = ["path_for", "refresh_from_document", "save", "try_open"]


def path_for(layout: project.ProjectLayout, document_path: str) -> str:
    """Where this document's working file belongs."""
    return layout.blend_for(document_path)


def save(layout: project.ProjectLayout, document_path: str) -> str | None:
    """Write the session as the working file; ``None`` on failure, which must never fail the
    operation that produced it."""
    destination = path_for(layout, document_path)
    try:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        # Suppressed: without it, merely opening a document would rewrite it (dirty git
        # status, bumped mtime, invalidated thumbnail).
        with sync.suppressed():
            bpy.ops.wm.save_as_mainfile(filepath=destination)
    except (OSError, RuntimeError):
        return None
    return destination


def refresh_from_document(scene: bpy.types.Scene) -> str | None:
    """Rebuild ``scene`` from its document; an error string means the cached objects were left
    alone. Always re-reads: a nested prefab or GLB can change without the document's stamp."""
    from ..document.prefab import PrefabDocumentError, loads
    from . import load

    state = store.read_state(scene)
    if state is None:
        return None
    if not os.path.isfile(state.path):
        return f"document is missing: {state.path}"

    located = project.locate(state.path)
    if located is None:
        return f"no asset project at or above {state.path}"

    try:
        with open(state.path, encoding="utf-8") as handle:
            document = loads(handle.read(), state.path)
    except (PrefabDocumentError, OSError) as error:
        return str(error)

    load.load_document(scene, document, state.path, located)
    return None


def try_open(layout: project.ProjectLayout, document_path: str) -> bool:
    """Open the working file; True only when it opened AND matches. False may already have
    replaced the session, so the caller materializes into whatever session it now has."""
    source = path_for(layout, document_path)
    if not os.path.isfile(source):
        return False

    try:
        bpy.ops.wm.open_mainfile(filepath=source)
    except RuntimeError:
        # A corrupt workfile is a cache miss, not an error.
        return False

    return is_current(bpy.context.scene, document_path)


def is_current(scene: bpy.types.Scene, document_path: str) -> bool:
    """Whether ``scene`` is a workfile for ``document_path`` that the document has not outrun."""
    state = store.read_state(scene)
    if state is None:
        return False

    if os.path.normcase(os.path.abspath(state.path)) != os.path.normcase(os.path.abspath(document_path)):
        # A different document's workfile: a miss, not one level shown under another's name.
        return False

    return not state.is_stale
