"""Writing a file so a reader never sees half of it.

Lives in ``document/`` and imports no ``bpy``: minting a sidecar and a document is pure file
work, and the unit tests exercise it outside Blender.
"""

from __future__ import annotations

import os
import shutil
import tempfile

__all__ = ["write_text"]


def write_text(path: str, text: str) -> None:
    """Temp file in the SAME directory then replace: ``os.replace`` is atomic only within one
    filesystem. The temp is created mode 0600, so an existing file's mode is copied onto it
    first, or every save would turn a 0644 file private."""
    directory = os.path.dirname(path)
    handle = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=directory, delete=False, suffix=".tmp"
        ) as handle:
            handle.write(text)
        if os.path.exists(path):
            shutil.copymode(path, handle.name)
        os.replace(handle.name, path)
    except BaseException:
        if handle is not None and os.path.exists(handle.name):
            os.unlink(handle.name)
        raise
