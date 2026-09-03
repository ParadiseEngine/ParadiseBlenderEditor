"""End-to-end live preview against the mock runtime.

Exercises the whole Blender-side chain -- protocol, transport, patch building -- without
needing the engine's ``--live`` listener, which is a separate change in the
``ParadiseGodotEditor`` repository.

The timer that normally drives updates is *not* used here: ``bpy.app.timers`` only fire from
Blender's event loop, which does not run under ``--background``. So the test calls the drain
step directly. That is a deliberate seam, not a workaround -- it means the coalescing logic is
testable, while the timer wiring itself is verified interactively.

Run with::

    blender --background --factory-startup --python tests/integration/test_live_preview.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

import bpy  # noqa: E402

import paradise_blender  # noqa: E402
from paradise_blender.authoring import authored_components  # noqa: E402
from paradise_blender.contract import component_ids  # noqa: E402
from paradise_blender.live import protocol, sync  # noqa: E402
from paradise_blender.live.transport import LiveConnection  # noqa: E402

PORT = 45199  # not the default, so a real session running locally is not disturbed


class FakeSession:
    """Stands in for ``live.session.LiveSession``.

    The real session also owns the runtime *process*; this test is about the message stream,
    so it supplies only the parts :mod:`paradise_blender.live.sync` actually uses.
    """

    def __init__(self, connection: LiveConnection) -> None:
        self.connection = connection
        self._seq = 0
        self.scene_name = "live_test"
        self.data_dir = os.path.join(tempfile.gettempdir(), "paradise_live_test")

    @property
    def connected(self) -> bool:
        return self.connection.connected

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def send(self, message) -> None:
        self.connection.send(message)

    def send_full_scene(self, scene) -> None:
        from paradise_blender.export.scene import build_level_data
        from paradise_blender.prefs import export_paths

        document = build_level_data(scene, export_paths(scene), export_assets=False)
        self.send(protocol.scene_full(self.next_seq(), document.to_json()))


DATA_DIR = os.path.join(tempfile.gettempdir(), "paradise_live_test")


def write_authoring_schema() -> None:
    """Put the REAL engine's authoring schema in this test's data directory.

    A data directory without one is a directory in which no component can be NAMED, and every
    export writes engine components (a name and a transform for every object, the environment for
    the scene) whose CLR type names are read out of it. That is the correct behaviour and exactly
    what a real project sets up by building its launcher; this test has to set it up too.
    """
    fixture = os.path.join(REPO, "tests", "fixtures", "engine-authoring-schema.json")
    with open(fixture, encoding="utf-8") as handle:
        printed = handle.read()
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "authoring-schema.json"), "w", encoding="utf-8") as file:
        file.write(printed)

    from paradise_blender.contract import authoring as contract_authoring
    contract_authoring._cache.clear()


def build_scene() -> bpy.types.Object:
    write_authoring_schema()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.paradise_project.data_dir = DATA_DIR
    scene.paradise_project.scene_name_override = "live_test"
    scene.paradise_project.export_on_save = False

    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0))
    obj = bpy.context.active_object
    obj.name = "Mover"
    obj.paradise.is_entity = True

    # It has to AUTHOR something. An object that says nothing beyond its name and its placement is
    # not exported at all — being marked as an entity and then given no components is not a
    # statement about the world — so a live-preview fixture that only set the flag would patch an
    # object the export had already dropped. An agent is the cheapest engine component with plain
    # fields, and this test is about the message stream rather than about what it carries.
    from paradise_blender.authoring import authored_components
    from paradise_blender.contract import authoring as contract_authoring
    from paradise_blender.contract import component_ids

    for component in contract_authoring.schema_for_data_dir(DATA_DIR).components:
        if component.id == component_ids.AGENT:
            authored_components.enable_component(obj, component)
            break
    return obj


def main() -> int:
    paradise_blender.register()
    obj = build_scene()
    scene = bpy.context.scene

    mock = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "tools", "mock_runtime.py"),
         "--port", str(PORT), "--once", "--verbose"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    failures: list[str] = []
    try:
        connection = LiveConnection(port=PORT)

        # The listener needs a moment to bind; retry rather than sleeping a fixed interval.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not connection.connect():
            time.sleep(0.1)

        if not connection.connected:
            print("FAIL: could not connect to the mock runtime")
            return 1
        print("ok   connected to the mock runtime")

        session = FakeSession(connection)
        session.send(protocol.hello(session.next_seq(), session.scene_name, session.data_dir))
        session.send_full_scene(scene)
        print("ok   sent hello + scene/full")

        sync.start(session)

        # Move the object and mark it dirty exactly as the depsgraph handler would, then drain.
        obj.location = (5.0, 6.0, 7.0)
        bpy.context.view_layer.update()
        sync._dirty_objects.add(obj.name)
        sync._drain()
        print("ok   drained a patch after moving the object")

        # AN OBJECT THAT STOPS AUTHORING ANYTHING must not just vanish from the patch stream.
        # A patch says nothing about what it omits, so if this only dropped the object from
        # `updated` the runtime would keep drawing something the scene no longer describes —
        # silently, until an unrelated structural change or a restart. It has to force a full
        # scene instead, which is the one message that CAN say "this is all there is".
        authored_components.disable_component(obj, component_ids.AGENT)
        sync._dirty_objects.add(obj.name)
        sync._drain()
        if not sync._needs_full_resync:
            failures.append("removing an object's last component did not force a full resync")
        else:
            print("ok   an object that stopped authoring forced a full resync")
        sync._drain()

        # Let the sender thread flush before the socket closes.
        time.sleep(0.5)
        session.send(protocol.bye(session.next_seq()))
        time.sleep(0.3)

        if connection.dropped_messages:
            failures.append(f"{connection.dropped_messages} message(s) were dropped")

        sync.stop()
        connection.close()

        output, _ = mock.communicate(timeout=15)
        print("\n--- mock runtime output ---")
        print(output.strip())
        print("---------------------------\n")

        for expected in (
            "hello: scene='live_test'",
            # TWO: the Mover, and the scene's own environment — which is an object like any other
            # since the lighting block became a component.
            "scene/full: 2 entities",
            "scene/patch: 1 changed",
            # The resync the removal forced. TWO full scenes: the handshake's, and this one.
            "scene/full: 1 entities",
            "bye",
        ):
            if expected not in output:
                failures.append(f"the runtime never saw: {expected!r}")

        if "! sequence gap" in output:
            failures.append("the runtime reported a sequence gap")
        if "! protocol mismatch" in output:
            failures.append("protocol version mismatch")

        # Blender (5, 6, 7) must arrive as contract (5, 7, -6). This is the axis conversion
        # surviving all the way through the live path, not just the file export.
        if "[5, 7, -6]" not in output.replace("5.0", "5").replace("7.0", "7").replace("-6.0", "-6"):
            failures.append("the patched world position was not converted into contract axes")
        else:
            print("ok   patched position arrived in contract axes: (5, 6, 7) -> [5, 7, -6]")

    finally:
        if mock.poll() is None:
            mock.kill()

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nLive preview round trip passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
