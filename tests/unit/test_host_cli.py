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


def test_the_cli_is_the_version_the_project_pins(tmp_path, monkeypatch):
    """A CLI older than the tree writes documents the runtime cannot read, and one that cannot
    read the manifest falls back to defaults and reports a cascade about everything except the
    version. So the version comes from the project, exactly as the release pipeline reads it."""
    root = tmp_path / "game"
    root.mkdir()
    (root / "Directory.Packages.props").write_text(
        "<Project>\n  <PropertyGroup>\n    <ParadiseVersion>0.42.0</ParadiseVersion>\n"
        "  </PropertyGroup>\n</Project>\n"
    )
    cached = tmp_path / "cache" / "0.42.0"
    cached.mkdir(parents=True)
    binary = cached / ("paradise.exe" if os.name == "nt" else "paradise")
    binary.write_text("")
    monkeypatch.setattr(host, "_TOOL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(host, "_preference", lambda name, default="": default)
    # Present, and deliberately NOT what gets used.
    monkeypatch.setattr(host.shutil, "which", lambda name: "/usr/local/bin/paradise")

    assert host.project_engine_version(str(root)) == "0.42.0"
    assert host.resolve_cli_command(str(root)) == [str(binary)]


def test_an_already_cached_version_is_not_fetched_again(tmp_path, monkeypatch):
    cached = tmp_path / "cache" / "0.42.0"
    cached.mkdir(parents=True)
    binary = cached / ("paradise.exe" if os.name == "nt" else "paradise")
    binary.write_text("")
    monkeypatch.setattr(host, "_TOOL_CACHE", str(tmp_path / "cache"))

    def refuse(*args, **kwargs):
        raise AssertionError("a cached version must not be fetched again")

    monkeypatch.setattr(host.subprocess, "run", refuse)

    assert host._versioned_cli("0.42.0") == [str(binary)]


def test_a_configured_cli_still_wins_over_the_pin(tmp_path, monkeypatch):
    """The escape hatch: pointing the addon at a source build is how the engine is worked on."""
    root = tmp_path / "game"
    root.mkdir()
    (root / "Directory.Packages.props").write_text("<ParadiseVersion>0.42.0</ParadiseVersion>")
    chosen = tmp_path / "built-paradise"
    chosen.write_text("")
    monkeypatch.setattr(
        host, "_preference", lambda name, default="": str(chosen) if name == "cli" else default
    )
    monkeypatch.setattr(host, "_versioned_cli", lambda version: ["never"])

    assert host.resolve_cli_command(str(root)) == [os.path.realpath(chosen)]


def test_a_pin_that_cannot_be_fetched_falls_through_rather_than_stopping_work(tmp_path, monkeypatch):
    """Offline, or a version never published. Falling back keeps the addon usable and says so;
    refusing outright would strand someone with no network and nothing to do."""
    root = tmp_path / "game"
    root.mkdir()
    (root / "Directory.Packages.props").write_text("<ParadiseVersion>9.9.9</ParadiseVersion>")
    monkeypatch.setattr(host, "_preference", lambda name, default="": default)
    monkeypatch.setattr(host, "_versioned_cli", lambda version: None)
    monkeypatch.setattr(host.shutil, "which", lambda name: "/usr/local/bin/paradise")

    assert host.resolve_cli_command(str(root)) == ["/usr/local/bin/paradise"]


def test_a_project_that_pins_nothing_uses_whatever_is_installed(tmp_path, monkeypatch):
    root = tmp_path / "game"
    root.mkdir()
    monkeypatch.setattr(host, "_preference", lambda name, default="": default)
    monkeypatch.setattr(host.shutil, "which", lambda name: "/usr/local/bin/paradise")

    assert host.project_engine_version(str(root)) is None
    assert host.resolve_cli_command(str(root)) == ["/usr/local/bin/paradise"]
