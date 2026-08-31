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

The session ADOPTS the workfile's path (``save_as_mainfile`` without ``copy``), so Blender's own
Ctrl+S updates the working file while "Save to Prefab Document" writes the document. Two saves
that mean different things, each going where it says.
"""

from __future__ import annotations

import os

import bpy

from ..document import project
from . import store

__all__ = ["path_for", "save", "try_open"]


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
        bpy.ops.wm.save_as_mainfile(filepath=destination)
    except (OSError, RuntimeError):
        return None
    return destination


def try_open(layout: project.ProjectLayout, document_path: str) -> bool:
    """Open this document's working file, if there is a current one.

    Returns True only when the file opened AND still matches the document. False means the caller
    should materialize from the document instead -- and it may already have replaced the session
    doing so, which is fine: the caller materializes into whatever session it now has.
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
