"""Ctrl+S writes the document too: a save that only wrote the disposable cache could lose work.

``save_pre``, NOT ``save_post``: the save refreshes the scene's stamp, and running before Blender
writes is what lands the fresh stamp IN the .blend. On ``save_post`` the workfile would carry a
stale stamp, be judged a miss on the next open, and be rebuilt (every GLB re-imported, camera and
selection lost). ``paradise_blender`` uses ``save_post`` only because its paths resolve against
``bpy.data.filepath``, which does not apply here.
"""

from __future__ import annotations

import contextlib

import bpy

from . import save, store

__all__ = ["clear_refusal", "refusal", "register_handler", "suppressed", "unregister_handler"]

#: Why the last save did not reach the document: a handler can neither open a dialog nor
#: cancel the save, so a refusal is reported after the fact or not at all.
REFUSAL_KEY = "paradise_sync_refusal"

#: Suppression depth for the addon's own .blend writes. NOT a recursion guard: ``workfile.save``
#: writes the .blend while OPENING a document, and an unsuppressed handler would rewrite the
#: document the instant you opened it. Do not delete on noticing nothing here calls itself.
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
    """Write every document-backed scene before Blender writes the .blend. ``@persistent`` or
    Blender drops the handler on file load and a day's edits silently never reach the tree."""
    if _suppression:
        return

    for scene in bpy.data.scenes:
        # A .blend that is not a workfile writes nothing; this makes the handler safe globally.
        if store.read_state(scene) is None:
            continue
        _sync(scene)


def _sync(scene: bpy.types.Scene) -> None:
    """Write one scene's document, recording rather than raising whatever stops it."""
    try:
        save.save_prefab(scene)
    except save.SaveError as error:
        # The document changed on disk. The .blend still saves, so the edit lives in the
        # workfile (but see #31: a reopen rematerializes and drops it).
        scene[REFUSAL_KEY] = str(error)
        print(f"[paradise_assets] save did not reach the document: {error}")
    except Exception as error:
        # A failed document write must never break saving the .blend.
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
