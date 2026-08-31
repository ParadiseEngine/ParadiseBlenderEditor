"""Build and Play, up to the point where a real program would start.

    blender --background --factory-startup --python tests/integration/test_play.py

Launching an actual game is not a test -- it needs a display, a built engine and a minute. But
everything BEFORE the process is exactly where this feature can go wrong, so the CLI and the
runtime are replaced with two scripts that record their argv and exit with whatever they are told.

The check that matters most is that a FAILED BUILD DOES NOT LAUNCH. The whole reason Play builds
first is that nothing else keeps `.editor/play/` fresh; a Play that launched anyway after a failed
build would show the author the previous build's world with nothing on screen saying so, which is
indistinguishable from the edit not having worked.

No project argument: this builds its own throwaway project, because it has to control what the
tools do and a real one would run the real ones.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import paradise_assets  # noqa: E402
from paradise_assets.document import project  # noqa: E402
from paradise_assets.materialize import store  # noqa: E402
from paradise_assets.play import host, ops as play_ops  # noqa: E402

failures: list[str] = []


def check(condition: bool, label: str) -> bool:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)
    return condition


# --------------------------------------------------------------------------------------
# A project, and two fake tools
# --------------------------------------------------------------------------------------

PREFAB = """schema_version = 1

[[objects]]

[[objects.components]]
id = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
type = "meta"
Guid = "33333333-4444-4555-8666-777777777777"
Name = "Level"
"""

#: Records argv and the bits of the environment under test, then exits with EXIT_CODE.
TOOL = """import json, os, sys
with open(os.environ["RECORD_TO"], "w", encoding="utf-8") as handle:
    json.dump({
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "ktx": os.environ.get("PARADISE_KTX_PATH"),
    }, handle)
print("build: 0 asset(s) into nowhere")
sys.exit(int(os.environ.get("EXIT_CODE", "0")))
"""


def write_tool(path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(TOOL)


def make_project(root: str) -> str:
    """A minimal asset project with one level. Returns the document path."""
    assets = os.path.join(root, "assets", "levels")
    os.makedirs(assets)
    with open(os.path.join(root, "assets", "project.toml"), "w", encoding="utf-8") as handle:
        handle.write('schema_version = 1\nname = "playtest"\n')
    document = os.path.join(assets, "arena.prefab")
    with open(document, "w", encoding="utf-8") as handle:
        handle.write(PREFAB)
    return document


def make_play_tree(root: str, *, with_config: bool) -> None:
    """What a successful build would have left behind."""
    play = os.path.join(root, ".editor", "play")
    os.makedirs(os.path.join(play, "levels"), exist_ok=True)
    with open(os.path.join(play, "levels", "arena.json"), "w", encoding="utf-8") as handle:
        handle.write("{}")
    with open(os.path.join(play, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "project": "playtest", "assets": []}, handle)
    if with_config:
        os.makedirs(os.path.join(play, "playtest"), exist_ok=True)
        with open(os.path.join(play, "playtest", "config.json"), "w", encoding="utf-8") as handle:
            handle.write("{}")


def configure(ktx: str = "") -> None:
    """Stand in for the addon's preferences.

    A `sys.path` import is not an INSTALLED addon, so `context.preferences.addons[...]` has no
    entry and there is no AddonPreferences to set -- which is exactly the case `get_preferences`
    returns None for. `host._preference` is the single seam every preference read goes through, so
    replacing it exercises the real code paths without needing the extension installed.
    """
    values = {"ktx_path": ktx, "runtime_arguments": "", "build_profile": "dev"}
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


# --------------------------------------------------------------------------------------
# The fakes are driven through host.run_cli / host.launch_runtime, with the interpreter
# in front: `cli` and `runtime_host` hold a .py path, so the argv prefix has to be
# python + script. Patching the resolvers is what lets the operators stay untouched.
# --------------------------------------------------------------------------------------


def patch_resolvers(cli_script: str, runtime_script: str) -> None:
    host.resolve_cli_command = lambda: [sys.executable, cli_script]
    host.resolve_runtime_command = lambda: [sys.executable, runtime_script]
    play_ops.resolve_cli_command = host.resolve_cli_command
    play_ops.resolve_runtime_command = host.resolve_runtime_command


def patch_missing_cli(runtime_script: str) -> None:
    host.resolve_cli_command = lambda: None
    host.resolve_runtime_command = lambda: [sys.executable, runtime_script]
    play_ops.resolve_cli_command = host.resolve_cli_command
    play_ops.resolve_runtime_command = host.resolve_runtime_command


def main() -> int:
    paradise_assets.register()
    original = (host.resolve_cli_command, host.resolve_runtime_command, host._preference)

    try:
        with tempfile.TemporaryDirectory() as work:
            root = os.path.join(work, "game")
            os.makedirs(root)
            document = make_project(root)
            layout = project.locate(document)

            cli_script = os.path.join(work, "fake_cli.py")
            runtime_script = os.path.join(work, "fake_runtime.py")
            write_tool(cli_script)
            write_tool(runtime_script)
            cli_log = os.path.join(work, "cli.json")
            runtime_log = os.path.join(work, "runtime.json")

            print("== a successful build launches the game ==")
            make_play_tree(root, with_config=True)
            configure(ktx=os.path.join(work, "ktx"))
            patch_resolvers(cli_script, runtime_script)
            open_document(document)

            os.environ.update({"RECORD_TO": cli_log, "EXIT_CODE": "0"})
            # The runtime records into its own file; both tools read RECORD_TO, so the launch
            # must see a different value than the build did.
            result = _play_with_runtime_record(runtime_log)
            check(result == {"FINISHED"}, f"Play finished ({result})")

            build = recorded(cli_log)
            check(build is not None, "the CLI ran")
            if build:
                check(
                    build["argv"] == ["assets", "build", "--profile", "dev", "--play"],
                    f"with the play build argv ({build['argv']})",
                )
                check(
                    os.path.realpath(build["cwd"]) == os.path.realpath(layout.root),
                    "in the project root",
                )
                check(build["ktx"] == os.path.realpath(os.path.join(work, "ktx")),
                      f"and with PARADISE_KTX_PATH set ({build['ktx']})")

            launch = recorded(runtime_log)
            check(launch is not None, "the runtime was launched")
            if launch:
                argv = launch["argv"]
                scene = argv[argv.index("--scene") + 1] if "--scene" in argv else ""
                check(
                    os.path.normcase(scene)
                    == os.path.normcase(os.path.join(root, ".editor", "play", "levels", "arena.json")),
                    f"--scene points at the built document ({os.path.basename(scene)})",
                )
                check("--config" in argv, "--config is derived when the build produced one")

            print("\n== a config the build did not produce is not invented ==")
            os.remove(os.path.join(root, ".editor", "play", "playtest", "config.json"))
            _reset(cli_log, runtime_log)
            _play_with_runtime_record(runtime_log)
            launch = recorded(runtime_log)
            check(
                launch is not None and "--config" not in launch["argv"],
                "no --config when there is no config file",
            )

            print("\n== a FAILED build does not launch ==")
            _reset(cli_log, runtime_log)
            os.environ["EXIT_CODE"] = "1"
            result = _play_with_runtime_record(runtime_log)
            check(result == {"CANCELLED"}, f"Play cancels on a failed build ({result})")
            check(recorded(cli_log) is not None, "the build was attempted")
            check(recorded(runtime_log) is None, "and the runtime was NOT launched")
            os.environ["EXIT_CODE"] = "0"

            print("\n== no CLI means no launch at all ==")
            _reset(cli_log, runtime_log)
            patch_missing_cli(runtime_script)
            result = _play_with_runtime_record(runtime_log)
            check(result == {"CANCELLED"}, f"Play cancels without a CLI ({result})")
            check(recorded(runtime_log) is None, "nothing was launched")
            patch_resolvers(cli_script, runtime_script)

            print("\n== the other verbs ==")
            _reset(cli_log, runtime_log)
            os.environ["RECORD_TO"] = cli_log
            bpy.ops.paradise_assets.build()
            build = recorded(cli_log)
            check(
                build is not None and build["argv"] == ["assets", "build", "--profile", "dev"],
                f"Build omits --play ({build['argv'] if build else None})",
            )

            _reset(cli_log, runtime_log)
            os.environ["RECORD_TO"] = cli_log
            bpy.ops.paradise_assets.clean()
            clean = recorded(cli_log)
            check(
                clean is not None and clean["argv"] == ["assets", "clean", "--keep-editor"],
                f"Clean keeps .editor by default ({clean['argv'] if clean else None})",
            )

            _reset(cli_log, runtime_log)
            os.environ["RECORD_TO"] = cli_log
            bpy.ops.paradise_assets.clean(editor_too=True)
            clean = recorded(cli_log)
            check(
                clean is not None and clean["argv"] == ["assets", "clean"],
                f"and drops the flag only when asked ({clean['argv'] if clean else None})",
            )

            _reset(cli_log, runtime_log)
            os.environ["RECORD_TO"] = cli_log
            bpy.ops.paradise_assets.verify()
            verify = recorded(cli_log)
            check(
                verify is not None and verify["argv"] == ["assets", "verify"],
                f"Verify runs the verify verb ({verify['argv'] if verify else None})",
            )
    finally:
        host.resolve_cli_command, host.resolve_runtime_command, host._preference = original
        for key in ("RECORD_TO", "EXIT_CODE", "RUNTIME_RECORD_TO"):
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


def _play_with_runtime_record(runtime_log: str):
    """Run Play, arranging for the launched fake to record somewhere of its own.

    The two fakes share one RECORD_TO, so the launch is given its own value by swapping the
    variable between the build (synchronous, already finished) and the launch. Detached or not,
    the child inherits the environment as it stands when Popen is called.
    """
    build_record = os.environ.get("RECORD_TO")

    real_launch = host.launch_runtime

    def launching(arguments, cwd):
        os.environ["RECORD_TO"] = runtime_log
        try:
            process, error = real_launch(arguments, cwd=cwd)
            if process is not None:
                process.wait(timeout=60)   # so the recording exists before we read it
            return process, error
        finally:
            if build_record is not None:
                os.environ["RECORD_TO"] = build_record

    play_ops.launch_runtime = launching
    try:
        return bpy.ops.paradise_assets.play()
    except RuntimeError:
        # An operator that reports ERROR and returns CANCELLED raises out of `bpy.ops`. That IS
        # the cancellation, so report it as one rather than letting it abort the suite -- the
        # failure cases below are the point of the test.
        return {"CANCELLED"}
    finally:
        play_ops.launch_runtime = real_launch


if __name__ == "__main__":
    sys.exit(main())
