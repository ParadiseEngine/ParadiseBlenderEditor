"""Externalize a GLB's embedded images as KTX2 sidecars.

**This is the missing half of the engine's texture contract.** The engine's glTF reader rejects
PNG/JPEG and reads textured meshes through an ``externalImageResolver`` that maps image URIs to
sidecar ``.ktx2`` files next to the GLB — the runtime hosts already pass that resolver. But
Blender's exporter can only EMBED images into a GLB; nothing produced the sidecar layout, so a
textured mesh either shipped megabytes of PNG the runtime cannot load, or shipped stripped.

:func:`externalize` closes the gap, run on each mesh GLB right after export:

1. every embedded PNG/JPEG image is transcoded to ``<glb stem>.<image name>.ktx2`` beside the
   GLB (through :mod:`.ktx`, which picks sRGB vs linear from the image NAME — names like
   ``T_Superhero_Male_Normal`` come through the exporter intact, which is what keeps normal and
   roughness maps out of sRGB);
2. the image entry is rewritten to an external ``uri`` pointing at the sidecar;
3. the now-orphaned bufferViews are dropped and the BIN chunk compacted — the point of the
   exercise is a small GLB, not a GLB that merely stopped referencing its dead bytes.

Compaction is the delicate part: bufferViews are referenced BY INDEX from accessors, images,
and sparse accessors, so removing entries renumbers everything after them. The rewrite walks
the whole document for ``bufferView`` keys rather than enumerating the schema, so an extension
that references a bufferView keeps working.
"""

from __future__ import annotations

import json
import os
import struct
import tempfile

from .. import log
from . import ktx

__all__ = ["externalize"]

_GLB_MAGIC = 0x46546C67
_CHUNK_JSON = 0x4E4F534A
_CHUNK_BIN = 0x004E4942
_IMAGE_MIME = {"image/png": ".png", "image/jpeg": ".jpg"}


def externalize(glb_path: str, transcoder: ktx.Transcoder) -> int:
    """Rewrite ``glb_path`` in place. Returns the number of images externalized.

    A GLB without embedded raster images is returned untouched (0). Failures transcode nothing
    and leave the GLB as exported — the runtime then reports the PNG rejection loudly, which
    beats a half-rewritten file.
    """
    with open(glb_path, "rb") as handle:
        blob = handle.read()

    magic, _version, _length = struct.unpack_from("<III", blob, 0)
    if magic != _GLB_MAGIC:
        log.warn(f"'{glb_path}' is not a GLB; textures left as exported.")
        return 0

    chunks: list[tuple[int, bytes]] = []
    offset = 12
    while offset < len(blob):
        chunk_length, chunk_type = struct.unpack_from("<II", blob, offset)
        chunks.append((chunk_type, blob[offset + 8 : offset + 8 + chunk_length]))
        offset += 8 + chunk_length

    document = json.loads(next(data for kind, data in chunks if kind == _CHUNK_JSON))
    bin_chunk = next((data for kind, data in chunks if kind == _CHUNK_BIN), b"")

    images = document.get("images") or []
    views = document.get("bufferViews") or []
    stem = os.path.splitext(os.path.basename(glb_path))[0]
    directory = os.path.dirname(os.path.abspath(glb_path))

    externalized = 0
    dead_views: set[int] = set()
    for index, image in enumerate(images):
        view_index = image.get("bufferView")
        extension = _IMAGE_MIME.get(image.get("mimeType", ""))
        if view_index is None or extension is None:
            continue

        view = views[view_index]
        start = view.get("byteOffset", 0)
        raw = bytes(bin_chunk[start : start + view["byteLength"]])

        # The sidecar keeps the IMAGE name: ktx.is_linear classifies by name tokens, and the
        # runtime resolver only needs the URI to exist next to the GLB.
        name = image.get("name") or f"image{index}"
        sidecar = f"{stem}.{_safe(name)}.ktx2"
        with tempfile.TemporaryDirectory(prefix="paradise_glbtex") as scratch:
            source = os.path.join(scratch, f"{_safe(name)}{extension}")
            with open(source, "wb") as handle:
                handle.write(raw)
            target = os.path.join(scratch, os.path.splitext(os.path.basename(source))[0] + ".ktx2")
            if not ktx.convert_image(source, transcoder, force=True):
                log.warn(f"'{glb_path}': transcode failed for image '{name}'; textures left as exported.")
                return 0
            os.replace(target, os.path.join(directory, sidecar))

        images[index] = {"uri": sidecar, "mimeType": "image/ktx2", "name": name}
        dead_views.add(view_index)
        externalized += 1

    if externalized == 0:
        return 0

    _compact(document, dead_views, bin_chunk, glb_path)
    log.info(f"'{os.path.basename(glb_path)}': {externalized} texture(s) -> KTX2 sidecars.")
    return externalized


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def _compact(document: dict, dead_views: set[int], bin_chunk: bytes, glb_path: str) -> None:
    """Drop dead bufferViews, rebuild the BIN chunk without their bytes, renumber references."""
    views = document.get("bufferViews") or []

    remap: dict[int, int] = {}
    kept: list[dict] = []
    pieces: list[bytes] = []
    cursor = 0
    for index, view in enumerate(views):
        if index in dead_views:
            continue
        remap[index] = len(kept)
        start = view.get("byteOffset", 0)
        data = bin_chunk[start : start + view["byteLength"]]
        # Preserve each view's 4-byte alignment: accessor component types require it, and the
        # exporter aligned the originals.
        padding = (-cursor) % 4
        pieces.append(b"\x00" * padding)
        cursor += padding
        rebuilt = dict(view)
        rebuilt["byteOffset"] = cursor
        kept.append(rebuilt)
        pieces.append(data)
        cursor += len(data)

    document["bufferViews"] = kept
    new_bin = b"".join(pieces)
    if document.get("buffers"):
        document["buffers"][0]["byteLength"] = len(new_bin)

    _renumber(document, remap)

    json_bytes = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_bytes += b" " * ((-len(json_bytes)) % 4)
    bin_padded = new_bin + b"\x00" * ((-len(new_bin)) % 4)

    total = 12 + 8 + len(json_bytes) + 8 + len(bin_padded)
    with open(glb_path, "wb") as handle:
        handle.write(struct.pack("<III", _GLB_MAGIC, 2, total))
        handle.write(struct.pack("<II", len(json_bytes), _CHUNK_JSON))
        handle.write(json_bytes)
        handle.write(struct.pack("<II", len(bin_padded), _CHUNK_BIN))
        handle.write(bin_padded)


def _renumber(node: object, remap: dict[int, int]) -> None:
    """Rewrite every ``bufferView`` reference anywhere in the document tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "bufferView" and isinstance(value, int):
                node[key] = remap[value]
            else:
                _renumber(value, remap)
    elif isinstance(node, list):
        for item in node:
            _renumber(item, remap)
