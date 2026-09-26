"""Just enough of the GLB container: its JSON chunk, and on request its binary chunk.

A generated prefab has to name a component, and a skinned mesh is a different component from a
static one in every game that has both (ShiningPie: ``SkinnedMesh`` and ``StaticMesh``). The
distinction is in the model, not in the schema, so it is read from the file: a glTF asset with a
non-empty ``skins`` array has a rig.

Questions like that read only the JSON chunk -- the binary chunk holding the geometry is never
touched, so they cost a few kilobytes on a multi-megabyte model. :func:`read_glb` is the one
exception, for an editable mesh, which rebuilds geometry primitive by primitive and cannot use
Blender's importer to do it (see ``materialize/editable_mesh.py``). Imports no ``bpy``.
"""

from __future__ import annotations

import json
import os
import struct

__all__ = ["has_skin", "read_glb", "read_json"]

_MAGIC = b"glTF"
_JSON_CHUNK = b"JSON"
_BIN_CHUNK = b"BIN\x00"
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


def read_glb(path: str) -> tuple[dict, bytes]:
    """The GLB's JSON chunk and its BIN chunk, or ``({}, b"")`` when the file is not a readable
    GLB. For the one caller that rebuilds geometry itself (an editable mesh): everything else
    asks the JSON a question and must not pay for the geometry."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return {}, b""

    if len(data) < _HEADER.size:
        return {}, b""
    magic, _version, length = _HEADER.unpack_from(data)
    if magic != _MAGIC or length > len(data):
        return {}, b""

    document: dict = {}
    binary = b""
    at = _HEADER.size
    while at + _CHUNK.size <= length:
        size, kind = _CHUNK.unpack_from(data, at)
        start = at + _CHUNK.size
        if start + size > length:
            return {}, b""
        if kind == _JSON_CHUNK and not document:
            try:
                document = json.loads(data[start:start + size].decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}, b""
        elif kind == _BIN_CHUNK and not binary:
            binary = data[start:start + size]
        at = start + size
    return (document, binary) if isinstance(document, dict) else ({}, b"")
