"""Build and Play, up to the point where a real program would start.

    blender --background --factory-startup --python tests/integration/test_play.py

Launching an actual game is not a test -- it needs a display, a built engine and a minute. But
everything BEFORE the process is exactly where this feature can go wrong, so the CLI is replaced
with a script that records its argv and exits with whatever it is told.

Play first builds assets, then the launcher, then runs ``paradise host play --no-assets``.
The fake records every stage and can fail assets, the launcher, or a later watch rebuild.
The addon must fail closed, including when the watch CLI prints an error but stays alive.
No real CLI, dotnet, launcher, or display is used.

No project argument: this builds its own throwaway project, because it has to control what the
tools do and a real one would run the real ones.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

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

#: Append one complete record per invocation; build must finish even when play should linger.
TOOL = """import json, os, sys, time
argv = sys.argv[1:]
with open(os.environ["RECORD_TO"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "argv": argv,
        "cwd": os.getcwd(),
        "ktx": os.environ.get("PARADISE_KTX_PATH"),
        "pid": os.getpid(),
    }) + "\\n")
stage = argv[:2]
mode = os.environ.get("FAIL_STAGE", "")
if stage == ["host", "build"]:
    if mode == "launcher":
        print("error: the launcher build failed: CS1002 ; expected", flush=True)
        sys.exit(1)
    print("Build succeeded. 0 Error(s)", flush=True)
elif stage == ["assets", "build"] and mode == "assets":
    print("error: asset conversion failed: arena texture missing", flush=True)
    sys.exit(1)
elif stage == ["host", "play"]:
    if mode == "watch-initial":
        print("Game.cs(4,2): error CS0246: InitialMissingType", flush=True)
        print("dotnet watch: Waiting for a file to change before restarting...", flush=True)
    else:
        print("build: 0 asset(s) into nowhere", flush=True)
        print("dotnet watch: Started", flush=True)
        if mode == "watch-rebuild":
            while not os.path.exists(os.environ["REBUILD_TRIGGER"]):
                time.sleep(0.05)
            print("Game.cs(8,2): error CS0103: RebuildMissingName", flush=True)
            print("dotnet watch: Waiting for a file to change before restarting...", flush=True)
    if os.environ.get("LINGER"):
        time.sleep(60)
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


def configure(ktx: str = "", profile: str = "dev") -> None:
    """Stand in for the addon's preferences.

    A `sys.path` import is not an INSTALLED addon, so `context.preferences.addons[...]` has no
    entry and there is no AddonPreferences to set -- which is exactly the case `get_preferences`
    returns None for. `host._preference` is the single seam every preference read goes through, so
    replacing it exercises the real code paths without needing the extension installed.
    """
    values = {"ktx_path": ktx, "build_profile": profile}
    host._preference = lambda name, default="": values.get(name, default) or default


def invocations(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as handle:
        lines = handle.readlines()
    # A concurrent append may not have finished the final JSON line yet.
    return [json.loads(line) for line in lines if line.endswith("\n")]


def recorded(path: str):
    records = invocations(path)
    return records[-1] if records else None


def open_document(document: str) -> None:
    """Put the scene in the state the operators poll for, without materializing anything."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    store.write_state(bpy.context.scene, document)


def patch_cli(cli_script: str | None) -> None:
    """The fake is driven through host.resolve_cli_command with the interpreter in front.

    Takes the project root the real one takes (it resolves the version a project pins) and
    ignores it: the fake IS the CLI under test, so there is nothing to version-match."""
    command = None if cli_script is None else [sys.executable, cli_script]
    host.resolve_cli_command = lambda project_root=None: command
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


def wait_for(path: str, seconds: float = 10.0, verb=("host", "play")):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for record in invocations(path):
            if record["argv"][:2] == list(verb):
                return record
        time.sleep(0.1)
    return None


def wait_for_exit(process, seconds: float = 10.0) -> bool:
    """Wait without querying the registry: the worker, not the panel, must kill a failed watch."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return True
        time.sleep(0.1)
    return False


def check_start_reporting(root: str) -> None:
    # report() is an RNA instance method, not a patchable attribute on the registered class.
    # Drive execute unbound to inspect its message; subsequent checks use real bpy operators.
    reports = []
    operator = SimpleNamespace(
        watch=False,
        report=lambda levels, message: reports.append((set(levels), message)),
        _background_wait=lambda: {"FINISHED"},
    )
    found = (SimpleNamespace(root=root), os.path.join(root, "arena.prefab"))
    with (
        patch.object(play_ops, "_playable", return_value=found),
        patch.object(play_ops, "_modal_possible", return_value=False),
        patch.object(session, "start", return_value=(SimpleNamespace(pid=42), None)),
    ):
        result = play_ops.PARADISE_ASSETS_OT_play.execute(operator, None)
    check(result == {"FINISHED"}, "startup observation returns the background result")
    check(reports == [({"INFO"}, "Build & Play started for arena.prefab")],
          f"spawning a session reports startup, not a running game ({reports})")


def check_late_modal_failure(root: str) -> None:
    """Drive the real modal methods without a UI or a three-minute wall-clock delay."""
    operator_type = play_ops.PARADISE_ASSETS_OT_play
    reports = []
    removed_timers = []
    process = SimpleNamespace(code=None, reason=None)
    process.poll = lambda: process.code
    timer = object()
    operator = SimpleNamespace(
        _process=process, _root=root, _timer=timer, _deadline=180.0,
        report=lambda levels, message: reports.append((set(levels), message)),
    )
    operator._report_exit = lambda: operator_type._report_exit(operator)
    operator._release = lambda context: operator_type._release(operator, context)
    context = SimpleNamespace(window_manager=SimpleNamespace(
        event_timer_remove=removed_timers.append,
    ))
    event = SimpleNamespace(type="TIMER")
    # Also exercise reporting from the operator's own session after the registry loses it.
    with patch.object(time, "monotonic", return_value=181.0), \
            patch.object(session, "process_for", return_value=None):
        status = operator_type.modal(operator, context, event)
        check(status == {"PASS_THROUGH"}, "modal still supervises beyond the former 180-second limit")
        check(operator._timer is timer and not removed_timers,
              "a live late session retains its reporting timer")
        check(not reports, "a live late session does not claim Playing success")
        process.code = 1
        process.reason = "exit 1: Game.cs: error CS0103: LateMissingName"
        status = operator_type.modal(operator, context, event)
        check(status == {"CANCELLED"}, "a late failure cancels the original modal operator")
        check(len(reports) == 1 and reports[0][0] == {"ERROR"}
              and "LateMissingName" in reports[0][1],
              f"late failure reports its own diagnostic despite missing registry entry ({reports})")
        check(operator._timer is None and removed_timers == [timer],
              "late failure removes its timer exactly once")
        operator_type.cancel(operator, context)
        check(removed_timers == [timer], "later cancellation does not remove the timer twice")


def main() -> int:
    paradise_assets.register()
    original = (host.resolve_cli_command, host._preference, play_ops.resolve_cli_command)
    env_keys = ("RECORD_TO", "FAIL_STAGE", "LINGER", "REBUILD_TRIGGER")
    original_env = {key: os.environ.get(key) for key in env_keys}

    try:
        with tempfile.TemporaryDirectory() as work:
            root = os.path.join(work, "game")
            os.makedirs(root)
            document = make_project(root)
            layout = project.locate(document)

            cli_script = os.path.join(work, "fake_cli.py")
            check_start_reporting(root)
            check_late_modal_failure(root)
            write_tool(cli_script)
            cli_log = os.path.join(work, "cli.jsonl")
            rebuild_trigger = os.path.join(work, "rebuild")

            print("== Play builds assets and launcher before playing the open document ==")
            configure(ktx=os.path.join(work, "ktx"))
            patch_cli(cli_script)
            open_document(document)
            os.environ.update({
                "RECORD_TO": cli_log, "FAIL_STAGE": "", "LINGER": "1",
                "REBUILD_TRIGGER": rebuild_trigger,
            })

            result = play(watch=False)
            check(result == {"FINISHED"}, f"Play finished ({result})")
            record = wait_for(cli_log)
            check(record is not None, "the play stage ran")
            stages = invocations(cli_log)
            check(
                [entry["argv"] for entry in stages] == [
                    ["assets", "build", "--editor", "--profile", "dev", "--project", layout.root],
                    ["host", "build", "--project", layout.root],
                    ["host", "play", "--no-assets", "--profile", "dev",
                     "--scene", document, "--project", layout.root],
                ],
                f"assets and launcher gates precede play ({[entry['argv'] for entry in stages]})",
            )
            for entry in stages:
                check(os.path.realpath(entry["cwd"]) == os.path.realpath(layout.root),
                      "each stage runs in the project root")
                check(entry["ktx"] == os.path.realpath(os.path.join(work, "ktx")),
                      "each stage receives the KTX environment")
            if record:
                argv = record["argv"]
                check(argv[:2] == ["host", "play"], f"with the host play verb ({argv})")
                check("--no-assets" in argv, "Play skips assets only after the explicit asset gate")
                check("--no-build" not in argv, "CLI may still catch a racing source edit")
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
            next_document = os.path.join(os.path.dirname(document), "second.prefab")
            with open(next_document, "w", encoding="utf-8") as handle:
                handle.write(PREFAB)
            store.write_state(bpy.context.scene, next_document)
            configure(ktx=os.path.join(work, "next-ktx"), profile="release")
            result = play(watch=True)
            check(result == {"FINISHED"}, f"Play finished ({result})")
            record = wait_for(cli_log)
            check(record is not None and "--watch" in record["argv"], "--watch reaches the CLI")
            if record:
                argv = record["argv"]
                check(argv[argv.index("--scene") + 1] == next_document,
                      "replacement reads the newly opened scene")
                check(argv[argv.index("--profile") + 1] == "release",
                      "replacement reads the newly selected profile")
                check(record["ktx"] == os.path.realpath(os.path.join(work, "next-ktx")),
                      "replacement reads the current environment preferences")
            check(
                [entry["argv"] for entry in invocations(cli_log)] == [
                    ["assets", "build", "--editor", "--profile", "release", "--project", layout.root],
                    ["host", "build", "--project", layout.root],
                    ["host", "play", "--no-assets", "--profile", "release",
                     "--scene", next_document, "--project", layout.root, "--watch"],
                ],
                "replacement rebuilds assets and launcher with fresh per-run arguments",
            )
            second = session.process_for(root)
            check(second is not None and second is not first, "a new session replaced the old")
            check(first is not None and first.poll() is not None, "and the old one was stopped")

            print("\n== Stop ends it ==")
            bpy.ops.paradise_assets.stop_play()
            check(not session.is_running(root), "nothing is running after Stop")
            check(session.exit_reason(root) is None, "and a Stop is not reported as a failure")

            print("\n== a FAILED build is reported ==")
            _reset(cli_log)
            configure()
            os.environ["FAIL_STAGE"] = "launcher"
            result = play(watch=False)
            check(result == {"CANCELLED"}, f"Play cancels when the CLI fails early ({result})")
            check(
                [entry["argv"][:2] for entry in invocations(cli_log)]
                == [["assets", "build"], ["host", "build"]],
                "a failed launcher build never invokes host play",
            )
            check(not session.is_running(root), "failed launcher build leaves no running session")
            reason = session.exit_reason(root)
            check(reason is not None and "CS1002" in reason, f"the panel gets the cause ({reason})")
            session.stop(root)

            print("\n== a non-watch asset failure is reported ==")
            _reset(cli_log)
            os.environ["FAIL_STAGE"] = "assets"
            result = play(watch=False)
            check(result == {"CANCELLED"}, f"asset failure cancels Play ({result})")
            check(
                [entry["argv"][:2] for entry in invocations(cli_log)] == [["assets", "build"]],
                "failed assets prevent both launcher build and play",
            )
            check(not session.is_running(root), "asset failure leaves no running session")
            reason = session.exit_reason(root)
            check(reason is not None and "arena texture missing" in reason,
                  f"asset failure retains its diagnostic ({reason})")
            session.stop(root)

            print("\n== an initial watch error cannot linger ==")
            _reset(cli_log)
            os.environ["FAIL_STAGE"] = "watch-initial"
            result = play(watch=True)
            check(result == {"CANCELLED"}, f"initial watch failure cancels Play ({result})")
            check(wait_for(cli_log) is not None, "the failing watch stage was invoked")
            check(not session.is_running(root), "initial watch failure is terminated despite LINGER")
            reason = session.exit_reason(root)
            check(reason is not None and "CS0246" in reason and "InitialMissingType" in reason,
                  f"initial watch failure retains its diagnostic ({reason})")
            session.stop(root)

            print("\n== a running watch is killed after a failed rebuild ==")
            _reset(cli_log, rebuild_trigger)
            os.environ["FAIL_STAGE"] = "watch-rebuild"
            result = play(watch=True)
            check(result == {"FINISHED"}, f"watch starts before the edit ({result})")
            check(wait_for(cli_log) is not None, "watch reached the play stage before the edit")
            running = session.process_for(root)
            check(running is not None, "watch is tracked before triggering the rebuild")
            with open(rebuild_trigger, "w", encoding="utf-8") as handle:
                handle.write("rebuild")
            if running is not None:
                check(wait_for_exit(running), "worker terminates failed watch without registry polling")
            check(not session.is_running(root), "failed rebuild leaves no running session")
            reason = session.exit_reason(root)
            check(reason is not None and "CS0103" in reason and "RebuildMissingName" in reason,
                  f"failed rebuild retains its diagnostic, not the termination signal ({reason})")
            session.stop(root)
            os.environ["FAIL_STAGE"] = ""
            os.environ.pop("LINGER", None)

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
        host.resolve_cli_command, host._preference, play_ops.resolve_cli_command = original
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
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
