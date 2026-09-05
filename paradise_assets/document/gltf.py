"""Just enough of the GLB container to tell a rigged model from a static one.

A generated prefab has to name a component, and a skinned mesh is a different component from a
static one in every game that has both (ShiningPie: ``SkinnedMesh`` and ``StaticMesh``). The
distinction is in the model, not in the schema, so it is read from the file: a glTF asset with a
non-empty ``skins`` array has a rig.

Only the JSON chunk is read -- the binary chunk holding the geometry is never touched, so this
costs a few kilobytes on a multi-megabyte model. Imports no ``bpy``: Blender's importer would
answer the same question by importing the whole model, which is minutes across a project.
"""

from __future__ import annotations

import json
import os
import struct

__all__ = ["has_skin", "read_json"]

_MAGIC = b"glTF"
_JSON_CHUNK = b"JSON"
_HEADER = struct.Struct("<4sII")
_CHUNK = struct.Struct("<I4s")

#: path -> (mtime_ns, size, answer). The mirror asks per poll and a model changes rarely.
_CACHE: dict[str, tuple[int, int, bool]] = {}


def has_skin(path: str) -> bool:
    """Whether the model at ``path`` is rigged. ``False`` for anything unreadable: a static
    component on a rigged model is a wrong preview, where guessing "rigged" on a file this
    cannot parse would author a component the game may not even declare."""
    try:
        stat = os.stat(path)
    except OSError:
        return False

    cached = _CACHE.get(path)
    if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
        return cached[2]

    document = read_json(path)
    skins = document.get("skins") if isinstance(document, dict) else None
    answer = isinstance(skins, list) and len(skins) > 0
    _CACHE[path] = (stat.st_mtime_ns, stat.st_size, answer)
    return answer


def read_json(path: str) -> dict:
    """The GLB's JSON chunk, or ``{}`` when the file is not a readable GLB."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(_HEADER.size)
            if len(header) < _HEADER.size:
                return {}
            magic, _version, _length = _HEADER.unpack(header)
            if magic != _MAGIC:
                return {}

            # The spec puts JSON first, but scanning the chunk list costs nothing and a reader
            # that assumed position would return {} for a legal file.
            while True:
                descriptor = handle.read(_CHUNK.size)
                if len(descriptor) < _CHUNK.size:
                    return {}
                size, kind = _CHUNK.unpack(descriptor)
                if kind.rstrip(b"\x00") != _JSON_CHUNK.rstrip(b"\x00") and kind != _JSON_CHUNK:
                    handle.seek(size, os.SEEK_CUR)
                    continue
                return json.loads(handle.read(size).decode("utf-8"))
    except (OSError, struct.error, ValueError, UnicodeDecodeError):
        return {}
