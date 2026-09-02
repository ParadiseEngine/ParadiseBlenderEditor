"""Loopback transport for live preview. Nothing here may block Blender's main thread (the
peer is a runtime that stalls for frames), so sends go onto a BOUNDED queue drained by a
thread; when the runtime stops draining, the oldest messages are dropped, which is safe because
a later ``scene/full`` supersedes them and the sequence numbers expose the gap.
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

#: A few seconds of edits at the default rate; beyond that older messages have no value.
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
            # The connect timeout also applies to sends and would abort a large scene/full.
            self._socket.settimeout(None)
            # Nagle would hold a small patch back waiting for the next one.
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
                self._dropped += 1

    def close(self) -> None:
        """Stop the workers and close the socket. Safe to call more than once."""
        if not self.connected and self._socket is None:
            return

        self._closing.set()
        self._connected.clear()
        # None wakes the sender's blocking get(); a full queue means it will see _closing anyway.
        with contextlib.suppress(queue.Full):
            self._outbox.put_nowait(None)

        if self._socket is not None:
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
                # Surface once and stop; the session layer notices `connected` went false.
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
            # TCP preserves no message boundaries: a recv may hold several lines or half of one.
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
