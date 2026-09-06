"""Build and Play, up to the point where a real program would start.

    blender --background --factory-startup --python tests/integration/test_play.py

Launching an actual game is not a test -- it needs a display, a built engine and a minute. But
everything BEFORE the process is exactly where this feature can go wrong, so the CLI is replaced
with a script that records its argv and exits with whatever it is told.

Play is ONE child: ``paradise host play`` builds the assets, builds the launcher and runs the game.
What the addon owes is the right verb on the right document from the right directory, a report
when that child dies early, and a Stop that ends it. The check that matters most is that a
failed build is REPORTED: a Play that quietly showed last build's world would be indistinguishable
from the edit not having worked.

No project argument: this builds its own throwaway project, because it has to control what the
tools do and a real one would run the real ones.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import paradise_assets
from paradise_assets.document import project
from paradise_assets.materialize import store
from paradise_assets.play import host, session
from paradise_assets.play import ops as play_ops

failures: list[str] = []


def check(condition: bool, label: str) -> bool:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)
    return condition


# --------------------------------------------------------------------------------------
# A project, and a fake CLI
# --------------------------------------------------------------------------------------

PREFAB = """schema_version = 1

[[objects]]

[[objects.components]]
id = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
type = "meta"
Guid = "33333333-4444-4555-8666-777777777777"
Name = "Level"
"""

#: Records argv and the bits of the environment under test, then exits with EXIT_CODE; with
#: LINGER set it stays alive like a running game until terminated.
TOOL = """import json, os, sys, time
with open(os.environ["RECORD_TO"], "w", encoding="utf-8") as handle:
    json.dump({
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "ktx": os.environ.get("PARADISE_KTX_PATH"),
    }, handle)
code = int(os.environ.get("EXIT_CODE", "0"))
if code:
    print("error: the launcher build failed: CS1002 ; expected")
else:
    print("build: 0 asset(s) into nowhere")
sys.stdout.flush()
if os.environ.get("LINGER"):
    time.sleep(60)
sys.exit(code)
"""


def write_tool(path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(TOOL)


def make_project(root: str) -> str:
    """A minimal asset project with one level. Returns the document path."""
    assets = os.path.join(root, "assets", "levels")
    os.makedirs(assets)
    with open(os.path.join(root, "assets", "project.toml"), "w", encoding="utf-8") as handle:
        handle.write('schema_version = 1\nname = "playtest"\n\n[host]\nproject = "Game/Game.csproj"\n')
    document = os.path.join(assets, "arena.prefab")
    with open(document, "w", encoding="utf-8") as handle:
        handle.write(PREFAB)
    return document


def configure(ktx: str = "") -> None:
    """Stand in for the addon's preferences.

    A `sys.path` import is not an INSTALLED addon, so `context.preferences.addons[...]` has no
    entry and there is no AddonPreferences to set -- which is exactly the case `get_preferences`
    returns None for. `host._preference` is the single seam every preference read goes through, so
    replacing it exercises the real code paths without needing the extension installed.
    """
    values = {"ktx_path": ktx, "build_profile": "dev"}
    host._preference = lambda name, default="": values.get(name, default) or default


def recorded(path: str):
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def open_document(document: str) -> None:
    """Put the scene in the state the operators poll for, without materializing anything."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    store.write_state(bpy.context.scene, document)


def patch_cli(cli_script: str | None) -> None:
    """The fake is driven through host.resolve_cli_command with the interpreter in front."""
    command = None if cli_script is None else [sys.executable, cli_script]
    host.resolve_cli_command = lambda: command
    play_ops.resolve_cli_command = host.resolve_cli_command


def call(operator, **properties):
    try:
        return operator(**properties)
    except RuntimeError:
        # An operator that reports ERROR and returns CANCELLED raises out of `bpy.ops`. That IS
        # the cancellation, so report it as one rather than letting it abort the suite -- the
        # failure cases below are the point of the test.
        return {"CANCELLED"}


def play(**properties):
    return call(bpy.ops.paradise_assets.play, **properties)


def wait_for(path: str, seconds: float = 10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if (record := recorded(path)) is not None:
            return record
        time.sleep(0.1)
    return None


def main() -> int:
    paradise_assets.register()
    original = (host.resolve_cli_command, host._preference)

    try:
        with tempfile.TemporaryDirectory() as work:
            root = os.path.join(work, "game")
            os.makedirs(root)
            document = make_project(root)
            layout = project.locate(document)

            cli_script = os.path.join(work, "fake_cli.py")
            write_tool(cli_script)
            cli_log = os.path.join(work, "cli.json")

            print("== Play runs `host play` on the open document and waits for it ==")
            configure(ktx=os.path.join(work, "ktx"))
            patch_cli(cli_script)
            open_document(document)
            os.environ.update({"RECORD_TO": cli_log, "EXIT_CODE": "0", "LINGER": "1"})

            result = play(watch=False)
            check(result == {"FINISHED"}, f"Play finished ({result})")
            record = wait_for(cli_log)
            check(record is not None, "the CLI ran")
            if record:
                argv = record["argv"]
                check(argv[:2] == ["host", "play"], f"with the host play verb ({argv})")
                check(
                    argv[argv.index("--scene") + 1] == document,
                    "--scene is the DOCUMENT (the CLI maps it to the play tree)",
                )
                check(
                    os.path.realpath(argv[argv.index("--project") + 1]) == os.path.realpath(layout.root),
                    "--project is the project root",
                )
                check(argv[argv.index("--profile") + 1] == "dev", "with the preference's profile")
                check("--watch" not in argv, "and no --watch unless asked")
                check(os.path.realpath(record["cwd"]) == os.path.realpath(layout.root), "in the project root")
                check(record["ktx"] == os.path.realpath(os.path.join(work, "ktx")),
                      f"and with PARADISE_KTX_PATH set ({record['ktx']})")
            check(session.is_running(root), "the session is running")
            first = session.process_for(root)

            print("\n== a second Play replaces the first ==")
            _reset(cli_log)
            result = play(watch=True)
            check(result == {"FINISHED"}, f"Play finished ({result})")
            record = wait_for(cli_log)
            check(record is not None and "--watch" in record["argv"], "--watch reaches the CLI")
            second = session.process_for(root)
            check(second is not None and second is not first, "a new session replaced the old")
            check(first is not None and first.poll() is not None, "and the old one was stopped")

            print("\n== Stop ends it ==")
            bpy.ops.paradise_assets.stop_play()
            check(not session.is_running(root), "nothing is running after Stop")
            check(session.exit_reason(root) is None, "and a Stop is not reported as a failure")

            print("\n== a FAILED build is reported ==")
            _reset(cli_log)
            os.environ["EXIT_CODE"] = "1"
            os.environ.pop("LINGER", None)
            result = play(watch=False)
            check(result == {"CANCELLED"}, f"Play cancels when the CLI fails early ({result})")
            check(recorded(cli_log) is not None, "the build was attempted")
            reason = session.exit_reason(root)
            check(reason is not None and "CS1002" in reason, f"the panel gets the cause ({reason})")
            os.environ["EXIT_CODE"] = "0"

            print("\n== no CLI means no launch at all ==")
            _reset(cli_log)
            patch_cli(None)
            result = play(watch=False)
            check(result == {"CANCELLED"}, f"Play cancels without a CLI ({result})")
            check(recorded(cli_log) is None, "nothing was launched")
            patch_cli(cli_script)

            print("\n== the other verbs ==")
            _reset(cli_log)
            bpy.ops.paradise_assets.build()
            build = recorded(cli_log)
            check(
                build is not None and build["argv"] == ["assets", "build", "--profile", "dev"],
                f"Build omits --editor ({build['argv'] if build else None})",
            )

            _reset(cli_log)
            # The fake writes no schema file, so the operator reports that and cancels; the
            # verb it ran is what is under test.
            result = call(bpy.ops.paradise_assets.build_schema)
            schema = recorded(cli_log)
            check(
                schema is not None and schema["argv"] == ["host", "build"],
                f"Build Game Schema is `host build` ({schema['argv'] if schema else None})",
            )
            check(result == {"CANCELLED"}, "and a build that dumped no schema is refused")

            _reset(cli_log)
            bpy.ops.paradise_assets.clean()
            clean = recorded(cli_log)
            check(
                clean is not None and clean["argv"] == ["assets", "clean", "--keep-editor"],
                f"Clean keeps .editor by default ({clean['argv'] if clean else None})",
            )

            _reset(cli_log)
            bpy.ops.paradise_assets.clean(editor_too=True)
            clean = recorded(cli_log)
            check(
                clean is not None and clean["argv"] == ["assets", "clean"],
                f"and drops the flag only when asked ({clean['argv'] if clean else None})",
            )

            _reset(cli_log)
            bpy.ops.paradise_assets.verify()
            verify = recorded(cli_log)
            check(
                verify is not None and verify["argv"] == ["assets", "verify"],
                f"Verify runs the verify verb ({verify['argv'] if verify else None})",
            )
    finally:
        session.stop_all()
        host.resolve_cli_command, host._preference = original
        for key in ("RECORD_TO", "EXIT_CODE", "LINGER"):
            os.environ.pop(key, None)
        paradise_assets.unregister()

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


def _reset(*logs: str) -> None:
    for path in logs:
        if os.path.isfile(path):
            os.remove(path)


if __name__ == "__main__":
    sys.exit(main())
