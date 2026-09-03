"""The one child environment every dotnet launch shares (#33)."""

from __future__ import annotations

import os

from paradise_blender.pipeline import dotnet


def test_the_dotnet_directory_leads_the_child_path(monkeypatch, tmp_path):
    fake = tmp_path / "sdk" / "dotnet"
    fake.parent.mkdir()
    fake.write_text("")
    monkeypatch.setattr(dotnet, "executable", lambda: str(fake))
    monkeypatch.setenv("PATH", "/usr/bin")

    environment = dotnet.subprocess_environment()

    assert environment["PATH"].split(os.pathsep)[0] == str(fake.parent.resolve())
    assert environment["DOTNET_CLI_DO_NOT_USE_MSBUILD_SERVER"] == "1"


def test_a_directory_already_on_path_is_not_repeated(monkeypatch, tmp_path):
    fake = tmp_path / "dotnet"
    fake.write_text("")
    monkeypatch.setattr(dotnet, "executable", lambda: str(fake))
    monkeypatch.setenv("PATH", str(tmp_path.resolve()))

    assert dotnet.subprocess_environment()["PATH"].count(str(tmp_path.resolve())) == 1


def test_no_sdk_leaves_path_alone(monkeypatch):
    monkeypatch.setattr(dotnet, "executable", lambda: None)
    monkeypatch.setenv("PATH", "/usr/bin")

    assert dotnet.subprocess_environment()["PATH"] == "/usr/bin"
