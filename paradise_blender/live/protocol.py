"""The live-preview wire protocol: NDJSON over loopback TCP, Blender as the CLIENT so it can
drop and reconnect (addon reload) without the preview window dying.

    hello -> (ready | error) ; scene/full ; scene/patch* ; asset/invalidate ; scene/full ; bye

Every message carries a monotonically increasing ``seq`` so the runtime can detect a dropped
patch and ask for one with ``{"type": "resync"}``, which the client answers with the next
``scene/full``. No current runtime listens; ``tools/mock_runtime.py`` is the executable spec.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "PROTOCOL_VERSION",
    "Message",
    "asset_invalidate",
    "bye",
    "decode",
    "encode",
    "hello",
    "scene_full",
    "scene_patch",
]

#: Bumped on any incompatible change; a half-understood patch stream is a subtly wrong scene.
PROTOCOL_VERSION = 1

Message = dict[str, Any]


def encode(message: Message) -> bytes:
    """One NDJSON line; ``json.dumps`` because this is a transport envelope, not a document."""
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes | str) -> Message | None:
    """Parse one line, or ``None``: a garbled line must not tear down a healthy session."""
    text = line.decode("utf-8") if isinstance(line, bytes) else line
    text = text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def hello(seq: int, scene_name: str, data_dir: str) -> Message:
    """Handshake. ``data_dir`` lets the runtime resolve the mesh and material paths that the
    scene payloads reference."""
    return {
        "type": "hello",
        "seq": seq,
        "protocol": PROTOCOL_VERSION,
        "scene": scene_name,
        "dataDir": data_dir,
    }


def scene_full(seq: int, level_data: dict[str, Any]) -> Message:
    """The complete level document, at handshake and after any structural change."""
    return {"type": "scene/full", "seq": seq, "scene": level_data}


def scene_patch(
    seq: int,
    updated: list[dict[str, Any]] | None = None,
    added: list[dict[str, Any]] | None = None,
    removed: list[str] | None = None,
) -> Message:
    """Incremental changes as whole objects, never field diffs (the runtime would need merge
    semantics for every contract field). ``removed`` carries object NAMES, the key the other
    lists are matched by."""
    return {
        "type": "scene/patch",
        "seq": seq,
        "updated": updated or [],
        "added": added or [],
        "removed": removed or [],
    }


def asset_invalidate(seq: int, fields: list[str]) -> Message:
    """Drop cached assets at these fields: a re-exported GLB keeps its path."""
    return {"type": "asset/invalidate", "seq": seq, "fields": fields}


def bye(seq: int) -> Message:
    """Clean shutdown, so the runtime can distinguish a deliberate stop from a crashed editor."""
    return {"type": "bye", "seq": seq}
