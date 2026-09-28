"""Just enough of a glTF model -- a ``.glb``, or a ``.gltf`` with its buffers beside it: its JSON.

A generated prefab has to name a component, and a skinned mesh is a different component from a
static one in every game that has both (ShiningPie: ``SkinnedMesh`` and ``StaticMesh``). The
distinction is in the model, not in the schema, so it is read from the file: a glTF asset with a
non-empty ``skins`` array has a rig.

Every question here reads only the JSON -- the binary data holding the geometry is never
touched, so it costs a few kilobytes on a multi-megabyte model.

A ``.gltf`` is the same asset as the GLB whose binary chunk is its buffers, and every reader here
hands it out as that GLB, the way the engine's pipeline reads it: buffers concatenated (each
4-byte aligned) into one buffer without a ``uri``, buffer views re-pointed into it. A buffer is a
``data:`` URI or a file named relative to the ``.gltf``, percent-decoded; a file that is absolute,
remote or outside ``assets/`` is refused (:class:`GltfError`), as the engine refuses it.
Imports no ``bpy``.
"""

from __future__ import annotations

import json
import os
import re
import struct
import urllib.parse

from . import project

__all__ = [
    "GltfError",
    "buffer_files",
    "has_skin",
    "is_gltf",
    "read_json",
    "reference_refusal",
]

_MAGIC = b"glTF"
_JSON_CHUNK = b"JSON"
_HEADER = struct.Struct("<4sII")
_CHUNK = struct.Struct("<I4s")

#: A URI with a scheme (``https:``, ``file:``, a Windows drive ``C:``) is not a relative file.
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_DATA = "data:"

#: path -> (mtime_ns, size, answer). The mirror asks per poll and a model changes rarely.
_CACHE: dict[str, tuple[int, int, bool]] = {}


class GltfError(ValueError):
    """A ``.gltf`` names data that may not be read. The message is for the author."""


def is_gltf(path: str) -> bool:
    return path.lower().endswith(".gltf")


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
    """The model's JSON as its GLB holds it, or ``{}`` when the file is not a readable model."""
    if is_gltf(path):
        document = _text_json(path)
        offsets = _offsets(document)
        return _merged(document, offsets) if offsets is not None else document
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


def buffer_files(path: str) -> list[str]:
    """The files holding the model's binary data besides ``path`` itself: a ``.gltf``'s buffer
    files that it may name, in buffer order; none for a GLB or a ``data:`` buffer."""
    if not is_gltf(path):
        return []
    found = []
    for buffer in _text_json(path).get("buffers") or []:
        uri = buffer.get("uri") if isinstance(buffer, dict) else None
        if isinstance(uri, str) and not _is_data(uri):
            try:
                found.append(_resolve(path, uri))
            except GltfError:
                continue
    return found


def reference_refusal(path: str) -> str | None:
    """Why the files the ``.gltf`` at ``path`` names may not be read, or ``None``: the first
    buffer or image uri that is absolute, remote or outside ``assets/``. ``None`` for a GLB."""
    if not is_gltf(path):
        return None
    document = _text_json(path)
    for kind in ("buffers", "images"):
        for item in document.get(kind) or []:
            uri = item.get("uri") if isinstance(item, dict) else None
            if isinstance(uri, str) and not _is_data(uri):
                try:
                    _resolve(path, uri)
                except GltfError as error:
                    return str(error)
    return None


# -- a .gltf as the GLB it stands for -----------------------------------------------------------------

def _text_json(path: str) -> dict:
    try:
        with open(path, "rb") as handle:
            document = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def _offsets(document: dict) -> list[int] | None:
    """Where each buffer starts in the one concatenated binary chunk, or ``None`` when the
    buffers are absent or malformed (left as they are, for a reader to refuse)."""
    buffers = document.get("buffers")
    if not isinstance(buffers, list) or not buffers:
        return None
    offsets, at = [], 0
    for buffer in buffers:
        length = buffer.get("byteLength") if isinstance(buffer, dict) else None
        if isinstance(length, bool) or not isinstance(length, int) or length < 0:
            return None
        at += -at % 4
        offsets.append(at)
        at += length
    offsets.append(at)
    return offsets


def _merged(document: dict, offsets: list[int]) -> dict:
    """``document`` with its buffers as one: the first buffer's name, extras and extensions, no
    ``uri``, the concatenated length; each buffer view moved to where its buffer landed."""
    merged = dict(document)
    first = document["buffers"][0]
    merged["buffers"] = [{**{key: value for key, value in first.items() if key != "uri"},
                          "byteLength": offsets[-1]}]
    views = []
    for view in document.get("bufferViews") or []:
        buffer = view.get("buffer") if isinstance(view, dict) else None
        offset = view.get("byteOffset", 0) if isinstance(view, dict) else None
        if (isinstance(buffer, int) and not isinstance(buffer, bool) and 0 <= buffer < len(offsets) - 1
                and isinstance(offset, int) and not isinstance(offset, bool)):
            view = {**view, "buffer": 0, "byteOffset": offsets[buffer] + offset}
        views.append(view)
    if "bufferViews" in document:
        merged["bufferViews"] = views
    return merged


def _is_data(uri: str) -> bool:
    return uri[:len(_DATA)].lower() == _DATA


def _resolve(path: str, uri: str) -> str:
    """The file the relative ``uri`` in the ``.gltf`` at ``path`` names, or raise: a model may
    only name files under the ``assets/`` it is in (beside it, outside a project)."""
    name = os.path.basename(path)
    if _SCHEME.match(uri) or uri.startswith(("/", "\\")):
        raise GltfError(f"{name} names {uri}, which is absolute or remote; a model may only name "
                        "files beside it under assets/")
    directory = os.path.dirname(os.path.abspath(path))
    # A backslash separates like a slash, as it does on Windows: "..\\x" must not slip out.
    relative = urllib.parse.unquote(uri).replace("\\", "/")
    target = os.path.normpath(os.path.join(directory, relative.replace("/", os.sep)))
    layout = project.locate(directory)
    root = os.path.normpath(layout.assets if layout is not None else directory)
    try:
        inside = os.path.commonpath((target, root)) == root
    except ValueError:   # another drive
        inside = False
    if not inside:
        raise GltfError(f"{name} names {uri}, which is outside {root}; a model may only name files "
                        "under assets/")
    return target

