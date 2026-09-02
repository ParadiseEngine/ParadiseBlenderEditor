"""The ``.blend`` a document is worked on in: ``.editor/blend/<path>.blend``.

**Derived and disposable**, like everything under ``.editor/``. Deleting the whole directory loses
nothing -- the next open re-materializes it from the document, which is the acceptance invariant
the asset plan states for that tree. So this is a CACHE, and it is only ever trusted while it can
prove it matches: the scene records which document it came from and that document's
``(mtime, size)`` stamp, and a workfile whose stamp has moved on is thrown away rather than shown.

What reusing it buys is not only speed, though re-importing 21 GLBs is most of the cost of opening
ShiningPie's district. It is that a working file keeps the things a document has no place for --
where the camera is, what is selected, a reference image parented to nothing -- and those survive
a reopen instead of being rebuilt into a default view every time.

Opening the ``.blend`` itself (File > Open, a recent file, double-click under ``.editor/blend/``)
still rematerializes from ``assets/``. The cache is what the session *is*; the objects in it are
what the documents last said, and an asset edited in another tool, a git pull, or a nested prefab
change would otherwise sit stale until someone pressed Reload. The camera and extras stay; the
document objects are rebuilt. See :func:`refresh_from_document`.

The session ADOPTS the workfile's path (``save_as_mainfile`` without ``copy``), so Blender's own
Ctrl+S updates the working file while "Save to Prefab Document" writes the document. Two saves
that mean different things, each going where it says.
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
    """Write the current session as this document's working file.

    Returns the path written, or ``None`` when it could not be -- a workfile is a convenience, and
    failing to write one must never fail the operation that produced it.
    """
    destination = path_for(layout, document_path)
    try:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        # Suppressed, because this write is the ADDON's, not the author's. `sync` hooks save_pre to
        # write the document on every save, and opening a document calls straight through here --
        # so without this, merely opening a file would rewrite it: dirty in `git status`, and an
        # mtime bumped for nothing, which also invalidates that prefab's rendered thumbnail.
        with sync.suppressed():
            bpy.ops.wm.save_as_mainfile(filepath=destination)
    except (OSError, RuntimeError):
        return None
    return destination


def refresh_from_document(scene: bpy.types.Scene) -> str | None:
    """Rebuild ``scene`` from the prefab it was cached from.

    Returns ``None`` when the scene is not a workfile, or when the rebuild succeeded. An error
    string means the document could not be read and the cached objects were left as they were.

    Always re-reads: a nested prefab, a GLB, or a material TOML can move without the scene
    document's ``(mtime, size)`` stamp changing, and those are the edits this exists to catch.
    """
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
    """Open this document's working file, if there is a current one.

    Returns True only when the file opened AND still matches the document. False means the caller
    should materialize from the document instead -- and it may already have replaced the session
    doing so, which is fine: the caller materializes into whatever session it now has.

    Opening the file fires ``load_post``, which rematerializes from ``assets/`` before this
    returns -- so a workfile that was stale on disk can still read as current afterwards. That
    is the point: the session (camera, extras) comes from the cache, the objects from the
    documents.
    """
    source = path_for(layout, document_path)
    if not os.path.isfile(source):
        return False

    try:
        bpy.ops.wm.open_mainfile(filepath=source)
    except RuntimeError:
        # A corrupt or unreadable workfile is a cache miss, not an error. The document is still
        # the source of truth and materializing from it is what the caller does next.
        return False

    return is_current(bpy.context.scene, document_path)


def is_current(scene: bpy.types.Scene, document_path: str) -> bool:
    """Whether ``scene`` is a workfile for ``document_path`` that the document has not outrun."""
    state = store.read_state(scene)
    if state is None:
        return False

    if os.path.normcase(os.path.abspath(state.path)) != os.path.normcase(os.path.abspath(document_path)):
        # A workfile for a different document, which means the naming changed under us. Treat it
        # as a miss rather than showing one level's contents under another's name.
        return False

    return not state.is_stale
