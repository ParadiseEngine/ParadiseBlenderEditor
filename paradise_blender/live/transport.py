"""Loopback socket transport for live preview.

The hard requirement: **nothing here may block Blender's main thread.** Blender's UI is
single-threaded, so a blocking ``send`` on a socket whose peer has stalled would freeze the
editor -- and the peer here is a game runtime that legitimately stalls for a frame or two
while it loads a mesh.

So sends go onto a queue drained by a background thread. The main thread only ever appends,
which is why :class:`LiveConnection.send` cannot block regardless of what the runtime is doing.

The queue is **bounded**. If the runtime stops draining, an unbounded queue would grow until
Blender ran out of memory; instead the oldest pending messages are dropped and the connection
is marked degraded. Dropping is safe for this protocol because a later ``scene/full`` supersedes
everything before it -- and the sequence numbers let the runtime notice the gap and ask for one.
"""

from __future__ import annotations

import contextlib
import queue
import socket
import threading
from collections.abc import Callable

from .. import log
from .protocol import Message, decode, encode

__all__ = ["LiveConnection"]

#: Enough to absorb a few seconds of edits at the default rate; beyond that the peer is
#: not keeping up and older messages have no value.
_MAX_PENDING = 256

_CONNECT_TIMEOUT_SECONDS = 2.0


class LiveConnection:
    """A non-blocking NDJSON client connection to the runtime."""

    def __init__(self, host: str = "127.0.0.1", port: int = 45123) -> None:
        self._host = host
        self._port = port
        self._socket: socket.socket | None = None
        self._outbox: queue.Queue[Message | None] = queue.Queue(maxsize=_MAX_PENDING)
        self._sender: threading.Thread | None = None
        self._receiver: threading.Thread | None = None
        self._connected = threading.Event()
        self._closing = threading.Event()
        self._dropped = 0
        self._on_message: Callable[[Message], None] | None = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def dropped_messages(self) -> int:
        """Messages discarded because the peer stopped keeping up."""
        return self._dropped

    def connect(self, on_message: Callable[[Message], None] | None = None) -> bool:
        """Connect and start the worker threads. Returns False if the runtime is not listening."""
        self._on_message = on_message
        try:
            self._socket = socket.create_connection(
                (self._host, self._port), timeout=_CONNECT_TIMEOUT_SECONDS
            )
            # Clear the timeout after connecting: it applies to sends and receives too, and a
            # 2-second send timeout would abort a large scene/full payload mid-write.
            self._socket.settimeout(None)
            # Live preview is latency-sensitive and its messages are small; Nagle's algorithm
            # would hold a patch back waiting to coalesce it with the next one.
            self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            self._socket = None
            return False

        self._closing.clear()
        self._connected.set()

        self._sender = threading.Thread(target=self._send_loop, name="paradise-live-send", daemon=True)
        self._sender.start()
        self._receiver = threading.Thread(
            target=self._receive_loop, name="paradise-live-recv", daemon=True
        )
        self._receiver.start()
        return True

    def send(self, message: Message) -> None:
        """Queue a message. Never blocks; drops the oldest pending message when full."""
        if not self.connected:
            return
        try:
            self._outbox.put_nowait(message)
        except queue.Full:
            try:
                self._outbox.get_nowait()
                self._dropped += 1
                self._outbox.put_nowait(message)
            except (queue.Empty, queue.Full):
                # The drain thread raced us; losing this one message is the correct outcome.
                self._dropped += 1

    def close(self) -> None:
        """Stop the workers and close the socket. Safe to call more than once."""
        if not self.connected and self._socket is None:
            return

        self._closing.set()
        self._connected.clear()
        # A None sentinel wakes the sender out of its blocking get(). A full queue means the
        # sender is already busy and will see _closing on its next iteration anyway.
        with contextlib.suppress(queue.Full):
            self._outbox.put_nowait(None)

        if self._socket is not None:
            # Both calls tolerate a socket the peer has already closed.
            with contextlib.suppress(OSError):
                self._socket.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                self._socket.close()
            self._socket = None

        for thread in (self._sender, self._receiver):
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=1.0)
        self._sender = None
        self._receiver = None

    def _send_loop(self) -> None:
        while not self._closing.is_set():
            try:
                message = self._outbox.get(timeout=0.25)
            except queue.Empty:
                continue

            if message is None or self._socket is None:
                break

            try:
                self._socket.sendall(encode(message))
            except OSError as error:
                # The runtime exited or the socket broke. Surface it once and stop; the
                # session layer notices `connected` went false and tears down cleanly.
                if not self._closing.is_set():
                    log.warn(f"Live preview connection lost: {error}")
                self._connected.clear()
                break

    def _receive_loop(self) -> None:
        buffer = b""
        while not self._closing.is_set() and self._socket is not None:
            try:
                chunk = self._socket.recv(4096)
            except OSError:
                break

            if not chunk:
                break  # peer closed

            buffer += chunk
            # Messages are newline-delimited, but TCP does not preserve message boundaries --
            # a single recv may hold several messages or half of one.
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                message = decode(line)
                if message is None:
                    continue
                if self._on_message is not None:
                    try:
                        self._on_message(message)
                    except Exception as error:  # a handler bug must not kill the thread
                        log.warn(f"Live preview message handler failed: {error}")

        self._connected.clear()
