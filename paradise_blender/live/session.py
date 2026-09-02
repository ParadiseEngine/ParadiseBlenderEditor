"""Live-preview session: one at a time, since the runtime binds a fixed port. Export fully
first (patches refer to meshes on disk), launch, poll for the listener, handshake, send the
full scene, subscribe. Teardown always runs, or a dangling depsgraph handler keeps queueing for
a dead socket. KNOWN GAP (#34): the handshake reply is never read and the wait sleeps on the
main thread.
"""

from __future__ import annotations

import time

import bpy

from .. import log
from ..export.scene import build_level_data, export_scene, resolve_scene_name
from ..play.host import launch_runtime
from ..prefs import export_paths, get_preferences
from . import protocol
from .transport import LiveConnection

__all__ = ["LiveSession", "current_session", "is_running", "start", "stop"]

#: Generous: a cold `dotnet run` compiles before it opens the port.
_STARTUP_TIMEOUT_SECONDS = 60.0
_POLL_INTERVAL_SECONDS = 0.25

_session: LiveSession | None = None


class LiveSession:
    """One live-preview session."""

    def __init__(self, scene: bpy.types.Scene) -> None:
        self.scene_name = resolve_scene_name(scene)
        self.data_dir = export_paths(scene).data_dir
        self.connection: LiveConnection | None = None
        self.runtime_pid: int | None = None
        self._seq = 0

    @property
    def connected(self) -> bool:
        return self.connection is not None and self.connection.connected

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def send(self, message) -> None:
        if self.connection is not None:
            self.connection.send(message)

    def send_full_scene(self, scene: bpy.types.Scene) -> None:
        """Push the whole document without re-exporting assets, which would stall Blender."""
        paths = export_paths(scene)
        document = build_level_data(scene, paths, export_assets=False)
        self.send(protocol.scene_full(self.next_seq(), document.to_json()))


def current_session() -> LiveSession | None:
    return _session


def is_running() -> bool:
    return _session is not None and _session.connected


def start(scene: bpy.types.Scene, operator=None) -> bool:
    """Start a preview session. Returns True once connected and synced."""
    global _session

    if _session is not None:
        log.warn("A live preview session is already running.", operator)
        return False

    preferences = get_preferences()
    port = preferences.live_port

    log.info("Exporting scene before starting live preview…", operator)
    if export_scene(scene, operator) is None:
        return False

    paths = export_paths(scene)
    scene_json = paths.level_data_output_path(resolve_scene_name(scene))

    session = LiveSession(scene)
    process = launch_runtime(["--scene", scene_json, "--live", str(port)], operator)
    if process is None:
        return False
    session.runtime_pid = process.pid

    connection = LiveConnection(port=port)
    if not _await_connection(connection, port, operator):
        return False

    session.connection = connection
    _session = session

    connection.send(protocol.hello(session.next_seq(), session.scene_name, session.data_dir))
    session.send_full_scene(scene)

    from . import sync

    sync.start(session)

    log.info(f"Live preview connected on port {port}.", operator)
    return True


def stop(operator=None) -> bool:
    """Stop the session and detach every handler. Idempotent."""
    global _session

    if _session is None:
        return False

    from . import sync

    sync.stop()

    if _session.connection is not None:
        if _session.connection.connected:
            _session.connection.send(protocol.bye(_session.next_seq()))
            # Let `bye` flush, or the runtime logs a dropped connection as a crash.
            time.sleep(0.05)
        dropped = _session.connection.dropped_messages
        if dropped:
            log.warn(
                f"{dropped} live-preview message(s) were dropped because the runtime could not "
                "keep up. Lower the update rate in the addon preferences if the preview lagged."
            )
        _session.connection.close()

    _session = None
    log.info("Live preview stopped.", operator)
    return True


def _await_connection(connection: LiveConnection, port: int, operator=None) -> bool:
    """Poll until the runtime's listener accepts, or time out."""
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        if connection.connect():
            return True
        time.sleep(_POLL_INTERVAL_SECONDS)

    log.error(
        f"The runtime did not open a live-preview listener on port {port} within "
        f"{int(_STARTUP_TIMEOUT_SECONDS)}s. This build of paradise-runtime may not support "
        "--live yet (the listener is a separate change in ParadiseGodotEditor). Play still "
        "works; see docs/live-preview.md.",
        operator,
    )
    return False
