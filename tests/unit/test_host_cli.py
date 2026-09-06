"""How the addon invokes the CLI.

The watcher is a long-lived run of Paradise.Cli.csproj. A second ``dotnet run`` of that same
project (Play / Build / Verify) used to compile through the MSBuild server the watcher still
owned, and died on MSB0001 ``Invalid node id specified``. The CLI therefore never builds on a
verb: its built dll runs straight under ``dotnet`` when it exists, ``dotnet run --no-build``
before then. The game's launcher is the CLI's own business (``paradise host play``).
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


def test_a_built_cli_runs_its_dll_directly(tmp_path, monkeypatch):
    # `dotnet run --no-build` evaluates the project first (half a second); the dll does not.
    project = tmp_path / "Paradise.Cli.csproj"
    project.write_text("<Project />\n")
    output = tmp_path / "bin" / "Debug" / "net10.0"
    output.mkdir(parents=True)
    (output / "paradise.dll").write_bytes(b"")
    monkeypatch.setattr(host.shutil, "which", lambda name: "/opt/homebrew/bin/dotnet")
    monkeypatch.setattr(
        host, "_preference", lambda name, default="": str(project) if name == "cli" else default
    )

    argv = host.resolve_cli_command()

    assert argv == ["/opt/homebrew/bin/dotnet", os.path.realpath(output / "paradise.dll")]


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
