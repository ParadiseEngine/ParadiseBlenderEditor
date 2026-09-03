"""How the addon invokes ``dotnet run`` for the CLI vs the game.

The watcher is a long-lived ``dotnet run`` of Paradise.Cli.csproj. A second ``dotnet run`` of
that same project (Play / Build / Verify) used to compile through the MSBuild server the
watcher still owned, and died on MSB0001 ``Invalid node id specified``. The CLI therefore
runs with ``--no-build``; the game launcher still builds, because Play is when a code change
should show up.
"""

from __future__ import annotations

import os

from paradise_assets.play import host


def test_cli_csproj_does_not_rebuild_on_every_verb(tmp_path, monkeypatch):
    project = tmp_path / "Paradise.Cli.csproj"
    project.write_text("<Project />\n")
    monkeypatch.setattr(host.shutil, "which", lambda name: "/opt/homebrew/bin/dotnet")
    monkeypatch.setattr(
        host, "_preference", lambda name, default="": str(project) if name == "cli" else default
    )

    argv = host.resolve_cli_command()

    assert argv is not None
    assert "--no-build" in argv
    assert argv[argv.index("--project") + 1] == os.path.realpath(project)


def test_runtime_csproj_still_builds(tmp_path, monkeypatch):
    project = tmp_path / "Game.csproj"
    project.write_text("<Project />\n")
    monkeypatch.setattr(host.shutil, "which", lambda name: "/opt/homebrew/bin/dotnet")

    argv = host._configured(str(project))

    assert argv is not None
    assert "--no-build" not in argv
    assert argv[argv.index("--project") + 1] == os.path.realpath(project)


def test_ensure_cli_built_skips_a_binary(monkeypatch):
    monkeypatch.setattr(host, "_preference", lambda name, default="": "/usr/bin/paradise")

    assert host.ensure_cli_built() is None


def test_ensure_cli_built_skips_when_the_output_is_already_there(tmp_path, monkeypatch):
    project = tmp_path / "Paradise.Cli.csproj"
    project.write_text("<Project />\n")
    output = tmp_path / "bin" / "Debug" / "net10.0"
    output.mkdir(parents=True)
    (output / "paradise.dll").write_bytes(b"")
    monkeypatch.setattr(host, "_preference", lambda name, default="": str(project))

    assert host.ensure_cli_built() is None


def test_dotnet_children_turn_the_msbuild_server_off(monkeypatch):
    monkeypatch.setattr(host, "_preference", lambda name, default="": "")

    env = host.subprocess_environment()

    assert env["DOTNET_CLI_DO_NOT_USE_MSBUILD_SERVER"] == "1"


def test_schema_build_runs_the_runtime_host_csproj(tmp_path, monkeypatch):
    # The schema is a function of the game's records: only the launcher's own build writes it.
    project = tmp_path / "Game.csproj"
    project.write_text("<Project />\n")
    monkeypatch.setattr(host.shutil, "which", lambda name: "/opt/homebrew/bin/dotnet")
    monkeypatch.setattr(
        host, "_preference", lambda name, default="": str(project) if name == "runtime_host" else default
    )

    argv = host.schema_build_stage()

    assert argv is not None
    assert argv[:2] == ["/opt/homebrew/bin/dotnet", "build"]
    assert argv[2] == os.path.realpath(project)


def test_a_prebuilt_runtime_host_has_no_schema_build(monkeypatch):
    monkeypatch.setattr(host.shutil, "which", lambda name: "/opt/homebrew/bin/dotnet")
    monkeypatch.setattr(
        host, "_preference", lambda name, default="": "/opt/game/ShiningPie" if name == "runtime_host" else default
    )

    assert host.schema_build_stage() is None
