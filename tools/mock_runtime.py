#!/usr/bin/env python3
"""A reference live-preview listener.

Speaks the server half of ``paradise_blender/live/protocol.py`` -- the half that will
eventually live in ``Paradise.Sample.Runtime`` as its ``--live <port>`` mode. Until that
lands in the ``ParadiseGodotEditor`` repository, this is what makes the Blender side of live
preview testable end to end.

It is also the executable specification of the protocol. When the C# listener is written, its
behaviour should match this: accept one client, validate the protocol version, maintain a
scene dictionary keyed by entity GUID, and notice sequence gaps.

Usage::

    python3 tools/mock_runtime.py [--port 45123] [--verbose] [--once]

``--once`` serves a single client and exits, which is what the integration test uses.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys

PROTOCOL_VERSION = 1


class MockRuntime:
    """Maintains the scene state a real runtime would render."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.entities: dict[str, dict] = {}
        self.scene_name: str | None = None
        self.data_dir: str | None = None
        self.last_seq = 0
        self.full_syncs = 0
        self.patches = 0
        self.gaps = 0

    def handle(self, message: dict) -> dict | None:
        """Process one message; returns a reply to send back, or None."""
        kind = message.get("type")
        seq = message.get("seq", 0)

        # A gap means a patch was dropped in transit or by the sender's bounded queue. A real
        # runtime would request a full resync rather than render a scene that has silently
        # diverged from the editor's.
        if seq and self.last_seq and seq > self.last_seq + 1:
            self.gaps += 1
            self._log(f"! sequence gap: expected {self.last_seq + 1}, got {seq}")
        self.last_seq = max(self.last_seq, seq)

        if kind == "hello":
            return self._on_hello(message)
        if kind == "scene/full":
            return self._on_full(message)
        if kind == "scene/patch":
            return self._on_patch(message)
        if kind == "asset/invalidate":
            self._log(f"asset/invalidate: {message.get('fields', [])}")
            return None
        if kind == "bye":
            self._log("bye — client disconnecting cleanly")
            return None

        self._log(f"? unknown message type: {kind!r}")
        return None

    def _on_hello(self, message: dict) -> dict:
        version = message.get("protocol")
        if version != PROTOCOL_VERSION:
            # Refuse rather than guess: a half-understood patch stream produces a subtly wrong
            # scene, which is worse than an obvious failure.
            self._log(
                f"! protocol mismatch: client speaks v{version}, "
                f"this listener speaks v{PROTOCOL_VERSION}"
            )
            return {"type": "error", "reason": "protocol-version-mismatch", "expected": PROTOCOL_VERSION}

        self.scene_name = message.get("scene")
        self.data_dir = message.get("dataDir")
        self._log(f"hello: scene={self.scene_name!r} dataDir={self.data_dir!r}")
        return {"type": "ready", "protocol": PROTOCOL_VERSION}

    def _on_full(self, message: dict) -> None:
        scene = message.get("scene") or {}
        entities = scene.get("Entities") or []
        self.entities = {e.get("EntityGuid", e.get("Id", "")): e for e in entities}
        self.full_syncs += 1
        self._log(f"scene/full: {len(self.entities)} entities")
        return None

    def _on_patch(self, message: dict) -> None:
        for entity in (message.get("added") or []) + (message.get("updated") or []):
            self.entities[entity.get("EntityGuid", entity.get("Id", ""))] = entity
        for guid in message.get("removed") or []:
            self.entities.pop(guid, None)

        self.patches += 1
        changed = len(message.get("updated") or []) + len(message.get("added") or [])
        removed = len(message.get("removed") or [])
        self._log(f"scene/patch: {changed} changed, {removed} removed ({len(self.entities)} total)")

        if self.verbose:
            for entity in message.get("updated") or []:
                world = entity.get("WorldMatrix") or []
                position = world[12:15] if len(world) >= 15 else "?"
                self._log(f"    {entity.get('Id')} -> {position}")
        return None

    def _log(self, text: str) -> None:
        print(f"[mock-runtime] {text}", flush=True)


def serve(port: int, verbose: bool, once: bool) -> int:
    runtime = MockRuntime(verbose)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        # Without SO_REUSEADDR a restart within the TIME_WAIT window fails to bind, which
        # makes iterating on the protocol needlessly painful.
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(1)
        print(f"[mock-runtime] listening on 127.0.0.1:{port}", flush=True)

        while True:
            connection, address = server.accept()
            print(f"[mock-runtime] client connected from {address[0]}:{address[1]}", flush=True)

            with connection:
                buffer = b""
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
                    # TCP does not preserve message boundaries: one recv may carry several
                    # messages or a fragment of one.
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if not line.strip():
                            continue
                        try:
                            message = json.loads(line)
                        except json.JSONDecodeError:
                            print(f"[mock-runtime] ! malformed line: {line[:120]!r}", flush=True)
                            continue

                        reply = runtime.handle(message)
                        if reply is not None:
                            connection.sendall((json.dumps(reply) + "\n").encode("utf-8"))

            print(
                f"[mock-runtime] client disconnected "
                f"({runtime.full_syncs} full syncs, {runtime.patches} patches, {runtime.gaps} gaps)",
                flush=True,
            )
            if once:
                return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=45123)
    parser.add_argument("--verbose", action="store_true", help="log each updated entity's position")
    parser.add_argument("--once", action="store_true", help="serve one client, then exit")
    args = parser.parse_args()

    try:
        return serve(args.port, args.verbose, args.once)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
