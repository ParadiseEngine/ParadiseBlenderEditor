"""The game as a supervised ``paradise host play`` child: the verb it runs and how its log reads."""

from __future__ import annotations

import os

from paradise_assets.play import session


def test_play_command_names_the_document_not_its_built_twin(monkeypatch):
    from paradise_assets.play import host

    monkeypatch.setattr(
        host, "_preference", lambda name, default="": "dev" if name == "build_profile" else default)

    argv = session.play_command(["paradise"], "/game", "/game/assets/levels/arena.prefab", watch=False)

    # The CLI maps assets/ to .editor/play/; this extension must not know that layout.
    assert argv == [
        "paradise", "host", "play",
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
