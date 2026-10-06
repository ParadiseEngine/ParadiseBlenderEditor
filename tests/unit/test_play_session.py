"""The game as a supervised ``paradise host play`` child: the verb it runs and how its log reads."""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time

import pytest

from paradise_assets.play import session


def test_play_command_names_the_document_not_its_built_twin(monkeypatch):
    from paradise_assets.play import host

    monkeypatch.setattr(
        host, "_preference", lambda name, default="": "dev" if name == "build_profile" else default)

    argv = session.play_command(["paradise"], "/game", "/game/assets/levels/arena.prefab", watch=False)

    # The CLI maps assets/ to .editor/play/; this extension must not know that layout.
    assert argv == [
        "paradise", "host", "play", "--no-assets",
        "--profile", "dev",
        "--scene", "/game/assets/levels/arena.prefab",
        "--project", "/game",
    ]


def test_watch_hands_the_launcher_to_dotnet_watch(monkeypatch):
    from paradise_assets.play import host

    monkeypatch.setattr(host, "_preference", lambda name, default="": default)

    argv = session.play_command(["paradise"], "/game", "/game/assets/levels/arena.prefab", watch=True)

    assert argv[-1] == "--watch"
    assert argv[argv.index("--profile") + 1] == "dev"


def test_log_paths_differ_per_checkout():
    first = session.log_path("/a/shiningpie")
    second = session.log_path("/b/shiningpie")

    assert first != second
    assert "shiningpie" in os.path.basename(first)


def test_first_error_line_prefers_the_cause_over_sdk_noise(tmp_path):
    log = tmp_path / "play.log"
    log.write_text(
        "/sdk/x.targets: warning NETSDK1234: something harmless\n"
        "build: 12 asset(s) into .editor/play\n"
        "Unhandled exception. System.IO.FileNotFoundException: props/crate.mesh\n"
        "   at ShiningPie.Game.SceneLoader.Load()\n",
        encoding="utf-8",
    )

    assert session.first_error_line(str(log)) == (
        "Unhandled exception. System.IO.FileNotFoundException: props/crate.mesh"
    )


def test_first_error_line_skips_a_successful_build_tally_before_a_crash(tmp_path):
    # The log is now the whole `host play` stream: a green launcher build's tally comes
    # before the game's crash, and "0 Error(s)" must not be reported as the cause.
    log = tmp_path / "play.log"
    log.write_text(
        "build: 231 asset(s) into .editor/play\n"
        "    0 Warning(s)\n"
        "    0 Error(s)\n"
        "Time Elapsed 00:00:04.61\n"
        "Unhandled exception. System.InvalidOperationException: the scene names no player\n",
        encoding="utf-8",
    )

    assert session.first_error_line(str(log)) == (
        "Unhandled exception. System.InvalidOperationException: the scene names no player"
    )


def test_first_error_line_names_an_msbuild_diagnostic_not_its_tally(tmp_path):
    log = tmp_path / "play.log"
    log.write_text(
        "play: sources changed since the last build, building\n"
        "/repo/Game/Sim.cs(12,5): error CS1002: ; expected [/repo/Game/Game.csproj]\n"
        "Build FAILED.\n"
        "    1 Error(s)\n",
        encoding="utf-8",
    )

    assert session.first_error_line(str(log)).startswith("/repo/Game/Sim.cs(12,5): error CS1002")


def test_first_error_line_falls_back_to_the_first_real_line(tmp_path):
    log = tmp_path / "play.log"
    log.write_text(
        "/sdk/x.targets: warning NETSDK1234: something harmless\n"
        "[Game] Cannot open a window here.\n",
        encoding="utf-8",
    )

    assert session.first_error_line(str(log)) == "[Game] Cannot open a window here."


def test_an_absent_log_has_no_error_line(tmp_path):
    assert session.first_error_line(str(tmp_path / "absent.log")) is None


def test_exit_reason_is_silent_for_a_clean_exit_and_a_stop():
    key = session._normalize("/game")
    try:
        session._EXITS[key] = (0, None)
        assert session.exit_reason("/game") is None
        session._EXITS[key] = (session.INTERRUPTED, None)
        assert session.exit_reason("/game") is None
        session._EXITS[key] = (1, "error CS1002: ; expected")
        assert session.exit_reason("/game") == "exit 1: error CS1002: ; expected"
    finally:
        session._EXITS.pop(key, None)


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    from paradise_assets.play import host

    script = tmp_path / "cli.py"
    script.write_text(
        """
import json, pathlib, sys, time
root = pathlib.Path.cwd()
config = json.loads((root / "behavior.json").read_text())
verb = " ".join(sys.argv[1:3])
with (root / "calls.jsonl").open("a") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
if config.get("linger_stage") == verb:
    (root / "waiting").touch()
    time.sleep(60)
if config.get("fail") == verb:
    print("noise\\n" * 20000, end="")
    print("error: " + verb + " failed", flush=True)
    sys.exit(7)
if config.get("custom_failure") == verb:
    print("[Game] Cannot open a window here.", flush=True)
    sys.exit(9)
if verb == "assets build":
    print("build: 12 asset(s) into .editor/play", flush=True)
if verb == "assets build" and "generated" in config:
    (root / "settings.json").write_text(config["generated"])
if verb == "host build":
    (root / "built.json").write_text((root / "settings.json").read_text())
if verb == "host play":
    if config.get("watch_failure") == "initial":
        print("dotnet watch : Build failed. Waiting for a file to change.", flush=True)
        time.sleep(60)
    (root / "played.json").write_text((root / "built.json").read_text())
    if config.get("watch_failure") == "rebuild":
        print("dotnet watch : Started", flush=True)
        while not (root / "rebuild").exists():
            time.sleep(0.02)
        print("game output\\n" * 10000, end="")
        print("Game.cs(1,1): error CS1002: ; expected", flush=True)
        time.sleep(60)
""",
        encoding="utf-8",
    )
    (tmp_path / "behavior.json").write_text("{}", encoding="utf-8")
    (tmp_path / "settings.json").write_text("original", encoding="utf-8")
    monkeypatch.setattr(host, "resolve_cli_command", lambda root: [sys.executable, str(script)])
    monkeypatch.setattr(host, "_build_stage", lambda root: None)
    monkeypatch.setattr(host, "_preference", lambda name, default="": default)
    monkeypatch.setattr(host, "subprocess_environment", lambda: dict(os.environ))
    try:
        yield tmp_path
    finally:
        session.stop(str(tmp_path))
        if os.path.exists(session.log_path(str(tmp_path))):
            os.unlink(session.log_path(str(tmp_path)))


def _behavior(root, **values):
    (root / "behavior.json").write_text(json.dumps(values), encoding="utf-8")


def _calls(root):
    return [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]


def _start(root, *, watch=False):
    process, error = session.start(str(root), str(root / "scene.prefab"), watch=watch)
    assert error is None
    assert process is not None
    return process


def _wait_for_file(path):
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), f"CLI never wrote {path}"


def test_build_stages_gate_play_and_use_the_same_project_profile(fake_cli, monkeypatch):
    from paradise_assets.play import host

    monkeypatch.setattr(host, "_preference", lambda name, default="": "release")
    process = _start(fake_cli)
    assert process.wait(timeout=10) == 0
    calls = _calls(fake_cli)
    assert [call[:2] for call in calls] == [
        ["assets", "build"], ["host", "build"], ["host", "play"],
    ]
    assert calls[0] == [
        "assets", "build", "--editor", "--profile", "release", "--project", str(fake_cli),
    ]
    assert calls[1] == ["host", "build", "--project", str(fake_cli)]
    assert calls[2][calls[2].index("--profile") + 1] == "release"
    assert "--no-assets" in calls[2]  # Only after the explicit successful asset stage.
    assert session.exit_reason(str(fake_cli)) is None


@pytest.mark.parametrize("stage", ["assets build", "host build"])
def test_failed_stage_never_launches_a_previous_build(fake_cli, stage):
    _behavior(fake_cli, fail=stage)
    process = _start(fake_cli)
    assert process.wait(timeout=10) == 7
    assert not (fake_cli / "played.json").exists()
    assert ["host", "play"] not in [call[:2] for call in _calls(fake_cli)]
    # The diagnostic follows more than the old 64 KiB head-only log limit.
    assert f"{stage} failed" in session.exit_reason(str(fake_cli))


def test_source_cli_build_is_also_a_gate(fake_cli, monkeypatch):
    from paradise_assets.play import host

    _behavior(fake_cli, fail="cli build")
    monkeypatch.setattr(
        host, "_build_stage", lambda root: [sys.executable, str(fake_cli / "cli.py"), "cli", "build"],
    )
    process = _start(fake_cli)
    assert process.wait(timeout=10) == 7
    assert [call[:2] for call in _calls(fake_cli)] == [["cli", "build"]]


def test_json_launcher_input_is_built_every_play_after_asset_generation(fake_cli):
    assert _start(fake_cli).wait(timeout=10) == 0
    assert (fake_cli / "played.json").read_text() == "original"
    # Model an untracked Content/EmbeddedResource input produced by the asset stage.
    _behavior(fake_cli, generated="new parameters")
    assert _start(fake_cli).wait(timeout=10) == 0
    assert (fake_cli / "played.json").read_text() == "new parameters"
    assert [call[:2] for call in _calls(fake_cli)].count(["host", "build"]) == 2


def test_initial_watch_failure_ends_an_otherwise_live_watcher(fake_cli):
    _behavior(fake_cli, watch_failure="initial")
    process = _start(fake_cli, watch=True)
    assert process.wait(timeout=10) == 1
    assert process._process.poll() is not None
    assert "Build failed" in process.reason
    assert not (fake_cli / "played.json").exists()


def test_watch_rebuild_failure_stops_the_stale_game_without_ui_polling(fake_cli):
    _behavior(fake_cli, watch_failure="rebuild")
    process = _start(fake_cli, watch=True)
    _wait_for_file(fake_cli / "played.json")
    assert process.poll() is None
    (fake_cli / "rebuild").touch()
    assert process.wait(timeout=10) == 1
    assert process._process.poll() is not None
    assert "CS1002" in session.exit_reason(str(fake_cli))


def test_stop_during_assets_does_not_advance_to_launcher_or_play(fake_cli):
    _behavior(fake_cli, linger_stage="assets build")
    process = _start(fake_cli)
    _wait_for_file(fake_cli / "waiting")
    assert session.stop(str(fake_cli)) is None
    assert process.poll() == session.INTERRUPTED
    assert [call[:2] for call in _calls(fake_cli)] == [["assets", "build"]]
    assert session.exit_reason(str(fake_cli)) is None


def test_replacement_is_refused_when_old_tree_cannot_be_reclaimed(fake_cli, monkeypatch):
    monkeypatch.setattr(session.process_tree, "stop", lambda root: "Earlier game could not be stopped")
    process, error = session.start(str(fake_cli), "scene.prefab", watch=False)
    assert process is None
    assert "could not be stopped" in error
    assert not (fake_cli / "calls.jsonl").exists()


@pytest.mark.parametrize("line", [
    "dotnet watch : Build failed. Waiting for a file to change.",
    "dotnet watch : Failed to build project.",
    "/repo/Game.cs(1,1): error CS1002: ; expected",
    "CSC : error CS0006: metadata file not found",
    "error NU1301: unable to load the service index",
    "Build FAILED.",
])
def test_watch_failure_markers(line):
    assert session._watch_failed(line)


@pytest.mark.parametrize("line", [
    "0 Error(s)",
    "dotnet watch : 0 Error(s)",
    "warning CS0168: the variable is never used",
    "dotnet watch : Started",
    "[Game] an error counter is now 1",
])
def test_watch_build_noise_is_not_a_failure(line):
    assert not session._watch_failed(line)


def _observer():
    observer = object.__new__(session.PlaySession)
    observer._pending = b""
    observer._diagnostic = observer._watch_error = observer._first_line = None
    observer._stopping = threading.Event()
    observer._playing = observer._watch = True
    return observer


def test_watch_diagnostic_split_across_writes_and_ansi_codes():
    observer = _observer()
    observer._read_output(io.BytesIO(b"\x1b[31mGame.cs: error CS"))
    assert observer._watch_error is None
    observer._read_output(io.BytesIO(b"1002: ; expected\x1b[0m\n"))
    assert observer._watch_error == "Game.cs: error CS1002: ; expected"


def test_record_failure_kills_only_the_new_unrecorded_child(fake_cli, monkeypatch):
    def cannot_record(*args, **kwargs):
        raise OSError("Cannot persist process ownership")

    _behavior(fake_cli, linger_stage="assets build")
    monkeypatch.setattr(session.process_tree, "record", cannot_record)
    process = _start(fake_cli)
    assert process.wait(timeout=10) == 1
    assert process._process.poll() is not None
    assert "Cannot persist process ownership" in process.reason


def test_stage_spawn_failure_is_reported_and_does_not_play(fake_cli, monkeypatch):
    popen = session.subprocess.Popen

    def fail_launcher(argv, **options):
        if argv[2:4] == ["host", "build"]:
            raise OSError("launcher could not start")
        return popen(argv, **options)

    monkeypatch.setattr(session.subprocess, "Popen", fail_launcher)
    process = _start(fake_cli)
    assert process.wait(timeout=10) == 1
    assert "launcher could not start" in process.reason
    assert [call[:2] for call in _calls(fake_cli)] == [["assets", "build"]]


def test_fallback_error_is_from_the_failed_stage_not_an_earlier_success(fake_cli):
    _behavior(fake_cli, custom_failure="host play")
    process = _start(fake_cli)
    assert process.wait(timeout=10) == 9
    assert process.reason == "exit 9: [Game] Cannot open a window here."


def test_watch_reader_drains_large_available_output_without_a_poll_delay():
    observer = _observer()
    observer._read_output(io.BytesIO(
        b"game output\n" * 20000 + b"Game.cs: error CS1002: ; expected\n",
    ))
    assert observer._watch_error == "Game.cs: error CS1002: ; expected"


def test_a_new_failed_play_stops_an_earlier_process_missing_from_the_registry(fake_cli):
    options = session.process_tree.launch_options(dict(os.environ))
    earlier = session.subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], **options,
    )
    try:
        session.process_tree.record(str(fake_cli), earlier, environment=options["env"])
        assert session._normalize(str(fake_cli)) not in session._SESSIONS
        _behavior(fake_cli, fail="assets build")
        assert _start(fake_cli).wait(timeout=10) == 7
        assert earlier.wait(timeout=2) != 0
        assert not (fake_cli / "played.json").exists()
    finally:
        if earlier.poll() is None:
            earlier.kill()
            earlier.wait(timeout=2)
