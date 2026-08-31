"""Blender's own save writes the document too.

``assets/`` is the source of truth and the ``.blend`` under ``.editor/blend/`` is a disposable
cache of one document in it. That premise makes a Ctrl+S that saves only the cache a Ctrl+S that
can LOSE WORK -- the cache is regenerable by definition, and this addon ships a Clean button that
deletes it. So a save writes both, and the document is the one that matters.

This shipped as an explicit button first, deliberately: sync-on-save means every experimental edit
reaches the committed source tree, and wiring it before the round trip was proven would have made
the addon's first bug a corrupted level. The round trip IS proven now -- nine of ShiningPie's
documents, byte for byte, in ``tests/integration/test_open_scene.py`` -- which was the stated
precondition. The button remains, for writing the document WITHOUT saving the .blend.

**``save_pre``, not ``save_post``, and the difference is load-bearing.** ``save.save_prefab`` ends
by refreshing the scene's ``(mtime, size)`` stamp; running before Blender writes means that fresh
stamp is what lands IN the .blend. On ``save_post`` the file would carry a stamp already stale
against the document just written, so the next open would judge the working file a miss and rebuild
from the document -- re-importing every GLB and discarding the camera and selection the working
file exists to preserve. Fixing that from ``save_post`` means calling ``save_mainfile`` from inside
the save machinery, which is a nested save re-entering the thing that called it.

(``paradise_blender``'s export hook does use ``save_post``, for a reason that does not apply here:
its output paths resolve against ``bpy.data.filepath``, which is only correct once a Save As has
completed. A document path is stored absolutely on the scene and never derived from where the
.blend lives.)
"""

from __future__ import annotations

import contextlib

import bpy

from . import save, store

__all__ = ["clear_refusal", "refusal", "register_handler", "suppressed", "unregister_handler"]

#: Why the last save did not reach the document, stored per scene for the panel to show.
#:
#: A handler can neither open a dialog nor cancel the save, so a refusal has to be reported after
#: the fact or not at all. The panel already WARNS that a stale document will refuse; this is what
#: lets it also say that one did.
REFUSAL_KEY = "paradise_sync_refusal"

#: Depth of "this .blend write is the addon's own, not the author pressing Ctrl+S".
#:
#: NOT a recursion guard -- there is no recursion to guard against, which is the whole advantage of
#: ``save_pre``. It exists because ``workfile.save`` writes the .blend as part of OPENING a
#: document, and an unsuppressed handler would therefore rewrite the document the instant you
#: opened it: `git status` dirty on a file you only looked at, and an mtime bumped for nothing,
#: which also invalidates that prefab's rendered thumbnail. Do not delete this on noticing that
#: nothing here calls itself.
_suppression = 0


@contextlib.contextmanager
def suppressed():
    """Mark the enclosed ``.blend`` write as the addon's own, so the handler ignores it."""
    global _suppression
    _suppression += 1
    try:
        yield
    finally:
        _suppression -= 1


def refusal(scene: bpy.types.Scene) -> str | None:
    """Why the last save did not reach this scene's document, if it did not."""
    value = scene.get(REFUSAL_KEY)
    return value if isinstance(value, str) and value else None


def clear_refusal(scene: bpy.types.Scene) -> None:
    if REFUSAL_KEY in scene:
        del scene[REFUSAL_KEY]


@bpy.app.handlers.persistent
def _on_save_pre(_file_path) -> None:
    """Write every document-backed scene back before Blender writes the .blend.

    ``@persistent`` is not decoration: without it Blender drops the handler on file load, and
    sync-on-save would silently stop working after the first document anyone opened -- which is
    exactly the failure CONVENTIONS and CLAUDE.md call out by name, because nothing about it looks
    broken until you notice a day's edits never reached the tree.
    """
    if _suppression:
        return

    for scene in bpy.data.scenes:
        # Two dictionary lookups, and it is what makes the handler safe to install globally: a
        # .blend that is not a working file for a document writes nothing at all.
        if store.read_state(scene) is None:
            continue
        _sync(scene)


def _sync(scene: bpy.types.Scene) -> None:
    """Write one scene's document, recording rather than raising whatever stops it."""
    try:
        save.save_prefab(scene)
    except save.SaveError as error:
        # The expected refusal: the document changed on disk, so writing would discard that
        # change. The .blend still saves, so the author's edit lives in the working file -- which
        # is what a cache is for, and why this is a refusal rather than a loss.
        scene[REFUSAL_KEY] = str(error)
        print(f"[paradise_assets] save did not reach the document: {error}")
    except Exception as error:
        # A failed document write must never break saving the .blend. The working file is the
        # fallback and Blender is about to write it.
        scene[REFUSAL_KEY] = f"{type(error).__name__}: {error}"
        print(f"[paradise_assets] save failed unexpectedly: {type(error).__name__}: {error}")
    else:
        clear_refusal(scene)


def register_handler() -> None:
    if _on_save_pre not in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.append(_on_save_pre)


def unregister_handler() -> None:
    if _on_save_pre in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.remove(_on_save_pre)
