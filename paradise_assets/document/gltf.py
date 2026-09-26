"""Just enough of a glTF model -- a ``.glb``, or a ``.gltf`` with its buffers beside it: its JSON,
and on request its binary data.

A generated prefab has to name a component, and a skinned mesh is a different component from a
static one in every game that has both (ShiningPie: ``SkinnedMesh`` and ``StaticMesh``). The
distinction is in the model, not in the schema, so it is read from the file: a glTF asset with a
non-empty ``skins`` array has a rig.

Questions like that read only the JSON -- the binary data holding the geometry is never touched,
so they cost a few kilobytes on a multi-megabyte model. :func:`read_glb` is the one exception,
for an editable mesh, which rebuilds geometry primitive by primitive and cannot use Blender's
importer to do it (see ``materialize/editable_mesh.py``).

A ``.gltf`` is the same asset as the GLB whose binary chunk is its buffers, and every reader here
hands it out as that GLB, the way the engine's pipeline reads it: buffers concatenated (each
4-byte aligned) into one buffer without a ``uri``, buffer views re-pointed into it. A buffer is a
``data:`` URI or a file named relative to the ``.gltf``, percent-decoded; a file that is absolute,
remote or outside ``assets/`` is refused (:class:`GltfError`), as the engine refuses it.
:func:`container_files` is the way back: what a rewritten model is as files on disk -- the GLB
itself, or the ``.gltf`` JSON and its one buffer file. Imports no ``bpy``.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import struct
import urllib.parse

from . import project

__all__ = [
    "GltfError",
    "buffer_files",
    "container_files",
    "has_skin",
    "is_gltf",
    "read_glb",
    "read_json",
    "reference_refusal",
    "rewrite_refusal",
]

_MAGIC = b"glTF"
_JSON_CHUNK = b"JSON"
_BIN_CHUNK = b"BIN\x00"
_HEADER = struct.Struct("<4sII")
_CHUNK = struct.Struct("<I4s")

#: A URI with a scheme (``https:``, ``file:``, a Windows drive ``C:``) is not a relative file.
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_DATA = "data:"

#: path -> (mtime_ns, size, answer). The mirror asks per poll and a model changes rarely.
_CACHE: dict[str, tuple[int, int, bool]] = {}


class GltfError(ValueError):
    """A ``.gltf`` names data that may not or cannot be read or written. The message is for the
    author."""


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


def read_glb(path: str) -> tuple[dict, bytes]:
    """The model's JSON and binary chunk as its GLB holds them, or ``({}, b"")`` when the file
    is not a readable model. For the one caller that rebuilds geometry itself (an editable mesh):
    everything else asks the JSON a question and must not pay for the geometry.

    Raises :class:`GltfError` when a ``.gltf``'s buffers cannot be read: a buffer the contract
    forbids, a file that is missing or shorter than its declared length."""
    if is_gltf(path):
        return _read_gltf(path)
    try:
        with open(path, "rb") as handle:
            return _parse_glb(handle.read())
    except OSError:
        return {}, b""


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


def rewrite_refusal(path: str) -> str | None:
    """Why the model at ``path`` cannot be rewritten as it is (:func:`container_files`), or
    ``None``. A GLB always can; a ``.gltf`` whose data is split across buffers cannot, since a
    rewrite puts it back into one."""
    if not is_gltf(path):
        return None
    buffers = _text_json(path).get("buffers") or []
    if len(buffers) > 1:
        return (f"it keeps its data in {len(buffers)} buffers, and a rewrite puts it back into one; "
                "export it again with a single buffer")
    return None


def container_files(path: str, glb: bytes) -> list[tuple[str, bytes]]:
    """The model ``glb`` (a GLB's bytes) as the files to write for the model at ``path``, the
    model itself last: ``[(path, glb)]`` for a GLB. A ``.gltf`` stays one: its JSON, with the
    binary chunk in the buffer the file on disk names -- the same ``data:`` URI kind or the same
    file (``<stem>.bin`` when it names none) -- and that file listed only when its bytes change.

    Raises :class:`GltfError` for a ``.gltf`` :func:`rewrite_refusal` refuses."""
    if not is_gltf(path):
        return [(path, glb)]
    refusal = rewrite_refusal(path)
    if refusal is not None:
        raise GltfError(refusal)
    document, binary = _parse_glb(glb)
    if not document:
        raise GltfError("the rewritten model is not a readable GLB")
    existing = _text_json(path).get("buffers") or [{}]
    uri = existing[0].get("uri") if isinstance(existing[0], dict) else None
    buffers = document.get("buffers") or [{}]
    data = binary[:buffers[0].get("byteLength", len(binary))]
    files: list[tuple[str, bytes]] = []
    if isinstance(uri, str) and _is_data(uri):
        uri = "data:application/octet-stream;base64," + base64.b64encode(data).decode("ascii")
    else:
        if not isinstance(uri, str):
            uri = urllib.parse.quote(os.path.splitext(os.path.basename(path))[0] + ".bin")
        target = _resolve(path, uri)
        if _bytes_of(target) != data:
            files.append((target, data))
    document["buffers"] = [{**buffers[0], "uri": uri, "byteLength": len(data)}]
    files.append((path, (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")))
    return files


# -- the GLB container -------------------------------------------------------------------------------

def _parse_glb(data: bytes) -> tuple[dict, bytes]:
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


def _read_gltf(path: str) -> tuple[dict, bytes]:
    document = _text_json(path)
    if not document:
        return {}, b""
    buffers = document.get("buffers")
    if not buffers:
        return document, b""
    offsets = _offsets(document)
    name = os.path.basename(path)
    if offsets is None:
        raise GltfError(f"{name}'s buffers do not each declare a byteLength")
    binary = bytearray()
    for index, buffer in enumerate(buffers):
        length = buffer["byteLength"]
        data = _buffer_bytes(path, index, buffer)
        if len(data) < length:
            raise GltfError(f"{name}'s buffer {index} holds {len(data)} bytes of the {length} it declares")
        binary += b"\x00" * (offsets[index] - len(binary))
        binary += data[:length]
    return _merged(document, offsets), bytes(binary)


def _buffer_bytes(path: str, index: int, buffer: dict) -> bytes:
    name = os.path.basename(path)
    uri = buffer.get("uri")
    if not isinstance(uri, str):
        raise GltfError(f"{name}'s buffer {index} names no uri; only a GLB has a buffer without one")
    if _is_data(uri):
        header, comma, payload = uri[len(_DATA):].partition(",")
        if not comma:
            raise GltfError(f"{name}'s buffer {index} is a malformed data URI")
        if not header.endswith(";base64"):
            return urllib.parse.unquote_to_bytes(payload)
        try:
            return base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as error:
            raise GltfError(f"{name}'s buffer {index} is a malformed base64 data URI") from error
    target = _resolve(path, uri)
    try:
        with open(target, "rb") as handle:
            return handle.read()
    except OSError as error:
        raise GltfError(
            f"{name}'s buffer {index} names {uri}, which could not be read: {error.strerror}") from error


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


def _bytes_of(path: str) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None
