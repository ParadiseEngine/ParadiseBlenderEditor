"""Live-preview session: the runtime process, the connection, and the change feed.

Owns one preview at a time (module-level :data:`_session`), because the runtime binds a fixed
port and two sessions would fight over it.

Startup sequence, and why it is ordered this way:

1. **Export once, fully.** The runtime needs meshes and materials on disk before it can render
   anything the patches refer to.
2. **Launch the runtime with ``--live <port>``** and let it come up.
3. **Poll for the listener** rather than sleeping a fixed interval -- first launch through
   ``dotnet run`` may build first and take seconds, while a warm binary is ready almost
   immediately.
4. **Handshake, then send the full scene.**
5. **Subscribe to depsgraph updates**, coalesced by :mod:`.sync`.

Teardown always runs, including when the runtime dies on its own: a dangling depsgraph handler
would keep queueing work for a socket nobody is reading.
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

#: How long to wait for the runtime to start listening. Generous because a cold `dotnet run`
#: compiles the host before it opens the port.
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
        """Push the whole level document.

        Assets are not re-exported here: this runs on every structural change, and rewriting
        every GLB each time an object is added would stall Blender for seconds.
        """
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
    session.runtime_pid = launch_runtime(
        ["--scene", scene_json, "--live", str(port)], operator
    )
    if session.runtime_pid is None:
        return False

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
            # Give the sender thread a moment to flush `bye` before the socket closes;
            # otherwise the runtime sees a dropped connection and logs it as a crash.
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
