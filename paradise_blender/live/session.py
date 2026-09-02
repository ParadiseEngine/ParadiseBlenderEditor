"""Live-preview session: one at a time, since the runtime binds a fixed port. Export fully
first (patches refer to meshes on disk), launch, poll for the listener, handshake, send the
full scene, subscribe. Teardown always runs, or a dangling depsgraph handler keeps queueing for
a dead socket.

Starting is a state machine (:class:`Startup`) stepped by the operator's modal timer, because a
cold ``dotnet run`` takes tens of seconds to open the port and a ``sleep`` loop here would hold
Blender's main thread for all of it. The runtime's reply to ``hello`` is READ: ``ready`` opens
the stream, ``error`` (a protocol mismatch) stops it, and a later ``resync`` from the runtime --
what it sends on noticing a sequence gap -- pushes the whole scene again.
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

__all__ = ["LiveSession", "Startup", "begin", "current_session", "is_running", "start", "stop"]

#: Generous: a cold `dotnet run` compiles before it opens the port.
_STARTUP_TIMEOUT_SECONDS = 60.0
_POLL_INTERVAL_SECONDS = 0.25
#: The listener is up by then; a reply that takes longer is a runtime that will never send one.
_HANDSHAKE_TIMEOUT_SECONDS = 10.0

_session: LiveSession | None = None


class LiveSession:
    """One live-preview session."""

    def __init__(self, scene: bpy.types.Scene) -> None:
        self.scene_name = resolve_scene_name(scene)
        self.data_dir = export_paths(scene).data_dir
        self.connection: LiveConnection | None = None
        self.runtime_pid: int | None = None
        self._seq = 0
        #: Set by the receive thread; read on the main thread.
        self.ready = False
        self.error: str | None = None
        self.resync_requested = False

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

    def on_message(self, message) -> None:
        """Runtime -> Blender, on the receive thread: only flags are set here; the timer that
        drains edits acts on them, since Blender data may only be touched from the main thread."""
        kind = message.get("type")
        if kind == "ready":
            self.ready = True
        elif kind == "error":
            self.error = str(message.get("reason") or "the runtime refused the session")
        elif kind == "resync":
            self.resync_requested = True


def current_session() -> LiveSession | None:
    return _session


def is_running() -> bool:
    return _session is not None and _session.connected


class Startup:
    """Launch, connect, handshake, sync -- one :meth:`step` per timer tick. ``done`` is set
    once the session is running or the attempt has failed; ``failed`` says which."""

    def __init__(self, scene: bpy.types.Scene, operator=None) -> None:
        self.done = False
        self.failed = False
        self._scene = scene
        self._operator = operator
        self._session: LiveSession | None = None
        self._connection: LiveConnection | None = None
        self._deadline = 0.0
        self._port = 0

    def launch(self) -> bool:
        """Export and launch the runtime; False when that already failed."""
        global _session

        if _session is not None:
            log.warn("A live preview session is already running.", self._operator)
            return self._fail()

        self._port = get_preferences().live_port

        log.info("Exporting scene before starting live preview…", self._operator)
        if export_scene(self._scene, self._operator) is None:
            return self._fail()

        paths = export_paths(self._scene)
        scene_json = paths.level_data_output_path(resolve_scene_name(self._scene))

        session = LiveSession(self._scene)
        process = launch_runtime(["--scene", scene_json, "--live", str(self._port)], self._operator)
        if process is None:
            return self._fail()
        session.runtime_pid = process.pid

        self._session = session
        self._connection = LiveConnection(port=self._port)
        self._deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        return True

    def step(self) -> None:
        """Advance: connect while the listener is not up, then wait for the handshake reply."""
        global _session

        if self.done:
            return
        session, connection = self._session, self._connection
        assert session is not None and connection is not None

        if not session.connected:
            if connection.connect(session.on_message):
                session.connection = connection
                connection.send(protocol.hello(session.next_seq(), session.scene_name, session.data_dir))
                self._deadline = time.monotonic() + _HANDSHAKE_TIMEOUT_SECONDS
            elif time.monotonic() >= self._deadline:
                log.error(
                    f"The runtime did not open a live-preview listener on port {self._port} within "
                    f"{int(_STARTUP_TIMEOUT_SECONDS)}s. This build of paradise-runtime may not "
                    "support --live yet (the listener is a separate change in ParadiseGodotEditor). "
                    "Play still works; see docs/live-preview.md.",
                    self._operator,
                )
                self._fail()
            return

        if session.error is not None:
            log.error(f"The runtime refused the live preview: {session.error}", self._operator)
            connection.close()
            self._fail()
            return
        if not session.ready:
            if time.monotonic() >= self._deadline:
                log.error(
                    "The runtime accepted the connection but never answered `hello`; it may "
                    "speak an older live-preview protocol.",
                    self._operator,
                )
                connection.close()
                self._fail()
            return

        _session = session
        session.send_full_scene(self._scene)

        from . import sync

        sync.start(session)
        log.info(f"Live preview connected on port {self._port}.", self._operator)
        self.done = True

    def _fail(self) -> bool:
        self.done = True
        self.failed = True
        return False


def begin(scene: bpy.types.Scene, operator=None) -> Startup | None:
    """Start a session for a modal operator to drive; ``None`` when launching already failed."""
    startup = Startup(scene, operator)
    return startup if startup.launch() else None


def start(scene: bpy.types.Scene, operator=None) -> bool:
    """Start a session, BLOCKING until connected and synced: for scripts and background Blender,
    which have no event loop for a modal. Returns True once running."""
    startup = begin(scene, operator)
    if startup is None:
        return False
    while not startup.done:
        startup.step()
        if not startup.done:
            time.sleep(_POLL_INTERVAL_SECONDS)
    return not startup.failed


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
