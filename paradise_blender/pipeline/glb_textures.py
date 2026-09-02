"""Externalize a GLB's embedded images as ``<glb stem>.<image name>.ktx2`` sidecars, the layout
the engine's ``externalImageResolver`` reads, since Blender can only embed. The image NAME
survives so :mod:`.ktx` can pick sRGB vs linear from it. Orphaned bufferViews are dropped and
the BIN compacted; references are renumbered by walking every ``bufferView`` key rather than
the schema, so extensions keep working. Transcoding was 36.5 s of a 40 s ShiningPie export
(~2.7 s per 2048² UASTC encode), hence :mod:`.cache`.
"""

from __future__ import annotations

import json
import os
import struct
import tempfile

from .. import log
from . import ktx
from .cache import ArtifactCache, digest

__all__ = ["external_image_uris", "externalize"]

CACHE_KIND = "ktx2"

_GLB_MAGIC = 0x46546C67
_CHUNK_JSON = 0x4E4F534A
_CHUNK_BIN = 0x004E4942
_IMAGE_MIME = {"image/png": ".png", "image/jpeg": ".jpg"}


def externalize(
    glb_path: str,
    transcoder: ktx.Transcoder,
    cache: ArtifactCache | None = None,
    force: bool = False,
) -> int:
    """Rewrite ``glb_path`` in place; returns images externalized. A failure leaves the GLB as
    exported, so the runtime's PNG rejection is loud rather than a half-rewritten file. A cache
    hit skips only the encode: the GLB was just re-exported with images embedded again."""
    parsed = _read_glb(glb_path)
    if parsed is None:
        log.warn(f"'{glb_path}' is not a GLB; textures left as exported.")
        return 0
    document, bin_chunk = parsed

    images = document.get("images") or []
    views = document.get("bufferViews") or []
    stem = os.path.splitext(os.path.basename(glb_path))[0]
    directory = os.path.dirname(os.path.abspath(glb_path))

    externalized = 0
    reused = 0
    dead_views: set[int] = set()
    taken: set[str] = set()
    for index, image in enumerate(images):
        view_index = image.get("bufferView")
        extension = _IMAGE_MIME.get(image.get("mimeType", ""))
        if view_index is None or extension is None:
            continue

        view = views[view_index]
        start = view.get("byteOffset", 0)
        raw = bytes(bin_chunk[start : start + view["byteLength"]])

        # The sidecar keeps the IMAGE name: ktx.is_linear classifies by name tokens. Two
        # images whose names differ only in unsafe characters would share one sidecar, so the
        # second gets the image index.
        name = image.get("name") or f"image{index}"
        safe = _safe(name)
        if safe in taken:
            safe = f"{safe}.{index}"
        taken.add(safe)
        sidecar = f"{stem}.{safe}.ktx2"
        sidecar_path = os.path.join(directory, sidecar)

        # Keyed on the argv the transcode will see, which the filename decides.
        source_name = f"{safe}{extension}"
        key = digest(raw, ktx.encode_signature(source_name, transcoder))

        if not force and cache is not None and cache.fetch(CACHE_KIND, key, sidecar_path):
            reused += 1
        else:
            with tempfile.TemporaryDirectory(prefix="paradise_glbtex") as scratch:
                source = os.path.join(scratch, source_name)
                with open(source, "wb") as handle:
                    handle.write(raw)
                target = os.path.join(scratch, os.path.splitext(source_name)[0] + ".ktx2")
                if not ktx.convert_image(source, transcoder, force=True):
                    log.warn(f"'{glb_path}': transcode failed for image '{name}'; textures left as exported.")
                    return 0
                os.replace(target, sidecar_path)
            if cache is not None:
                cache.store(CACHE_KIND, key, sidecar_path)

        images[index] = {"uri": sidecar, "mimeType": "image/ktx2", "name": name}
        dead_views.add(view_index)
        externalized += 1

    if externalized == 0:
        return 0

    _compact(document, dead_views, bin_chunk, glb_path)
    detail = f" ({reused} reused from cache)" if reused else ""
    log.info(f"'{os.path.basename(glb_path)}': {externalized} texture(s) -> KTX2 sidecars{detail}.")
    return externalized


def external_image_uris(glb_path: str) -> list[str]:
    """A GLB's external image URIs (its sidecars). An unreadable file returns nothing, which a
    deleting caller must read as "no information", never "references nothing"."""
    parsed = _read_glb(glb_path)
    if parsed is None:
        return []
    document, _bin_chunk = parsed
    return [image["uri"] for image in document.get("images") or [] if image.get("uri")]


def _read_glb(glb_path: str) -> tuple[dict, bytes] | None:
    """A GLB's JSON document and BIN chunk, or ``None`` if it is not a readable GLB."""
    try:
        with open(glb_path, "rb") as handle:
            blob = handle.read()
        if len(blob) < 12 or struct.unpack_from("<III", blob, 0)[0] != _GLB_MAGIC:
            return None

        chunks: list[tuple[int, bytes]] = []
        offset = 12
        while offset < len(blob):
            chunk_length, chunk_type = struct.unpack_from("<II", blob, offset)
            chunks.append((chunk_type, blob[offset + 8 : offset + 8 + chunk_length]))
            offset += 8 + chunk_length

        document = json.loads(next(data for kind, data in chunks if kind == _CHUNK_JSON))
    except (OSError, ValueError, struct.error, StopIteration):
        return None

    if not isinstance(document, dict):
        return None
    return document, next((data for kind, data in chunks if kind == _CHUNK_BIN), b"")


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
        # Accessor component types require 4-byte alignment.
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
