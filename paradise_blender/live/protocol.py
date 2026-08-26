"""The live-preview wire protocol.

Newline-delimited JSON over a loopback TCP socket. Blender is the **client**; the runtime
listens. That direction is deliberate: the runtime already owns a window and an event loop, so
making it the server means Blender can connect, drop, and reconnect (across an addon reload,
say) without the preview window dying.

NDJSON rather than a binary format because the payloads are already the contract's JSON, the
volume is tiny (tens of entities at ~10 Hz), and being able to ``nc`` the port and read what
Blender is sending is worth more here than bytes on the wire.

Message flow::

    Blender                                   runtime
       |-- hello ----------------------------->|   version handshake
       |<-- ready ----------------------------|   (or `error` and close)
       |-- scene/full ------------------------>|   whole LevelData
       |-- scene/patch ----------------------->|   changed entities only
       |-- scene/patch ----------------------->|
       |-- asset/invalidate ------------------>|   a GLB or material was rewritten
       |-- scene/full ------------------------>|   after a structural change
       |-- bye ------------------------------->|

Every message is a JSON object with a ``type`` and a monotonically increasing ``seq``. The
sequence number exists so the runtime can detect a dropped or reordered patch and ask for a
full resync rather than rendering a scene that silently diverged from Blender's.

**This is the Blender half.** The runtime's ``--live`` listener is a separate change in the
``ParadiseGodotEditor`` repository (workspace rule: no commit spans repos). Until it lands,
``tools/mock_runtime.py`` implements this protocol and is what the tests drive.
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

#: Bumped on any incompatible change. The runtime rejects a version it does not understand
#: rather than guessing, because a half-understood patch stream produces a subtly wrong scene
#: instead of an obvious failure.
PROTOCOL_VERSION = 1

Message = dict[str, Any]


def encode(message: Message) -> bytes:
    """Serialize one message as a single NDJSON line.

    Uses ``json.dumps`` rather than the contract writer: this is a transport envelope, not a
    contract document, so compactness matters more than float32 formatting fidelity. The
    contract payloads nested inside have already been built by the schema layer.
    """
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes | str) -> Message | None:
    """Parse one NDJSON line, or ``None`` if it is blank or malformed.

    Returning ``None`` instead of raising keeps a garbled line from tearing down an otherwise
    healthy session -- the reader logs and continues.
    """
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
    """The complete level document.

    Sent at handshake and whenever a change is structural enough that a patch would be
    ambiguous -- an entity added or removed, a material rewritten, the environment changed.
    """
    return {"type": "scene/full", "seq": seq, "scene": level_data}


def scene_patch(
    seq: int,
    updated: list[dict[str, Any]] | None = None,
    added: list[dict[str, Any]] | None = None,
    removed: list[str] | None = None,
) -> Message:
    """Incremental entity changes.

    ``updated`` and ``added`` carry whole objects -- component arrays -- rather than field diffs:
    an object's document is small, and a field-level diff would need the runtime to implement
    merge semantics for every contract field, which is far more surface for the two sides to
    disagree on.

    ``removed`` carries object NAMES, read from each object's Name component. It used to carry
    entity GUIDs, on the grounds that a GUID is stable where a name is not -- true, and it went
    with the entity record in schema v5. Nothing else identified an object, and inventing a second
    identity for the live link alone would mean a value that exists in the patch stream and in no
    exported document.
    """
    return {
        "type": "scene/patch",
        "seq": seq,
        "updated": updated or [],
        "added": added or [],
        "removed": removed or [],
    }


def asset_invalidate(seq: int, fields: list[str]) -> Message:
    """Tell the runtime to drop cached assets for these data-relative contract fields.

    Needed because a re-exported GLB or material keeps the same path; without an explicit
    invalidation the runtime would keep serving the version it loaded at startup.
    """
    return {"type": "asset/invalidate", "seq": seq, "fields": fields}


def bye(seq: int) -> Message:
    """Clean shutdown, so the runtime can distinguish a deliberate stop from a crashed editor."""
    return {"type": "bye", "seq": seq}
