"""A ``.blend``/``.fbx`` model is read through the GLB the pipeline converted it to.

The viewport, Make Mesh Editable and the clip settings all read that GLB, so the one question
that matters is whether the file under ``.editor/converted/`` is the conversion of the source as
it is NOW: the stamp inside it names the source bytes, and a saved ``.blend`` must stop matching.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from test_gltf import glb

from paradise_assets.document import glb_clips, model_source
from paradise_assets.document.project import ProjectLayout

GUID = "97291278-960a-59b4-993d-39bf82c47b29"

_MTIME = [1_700_000_000]


@pytest.fixture(autouse=True)
def clean_caches():
    model_source._CACHE.clear()
    glb_clips._RIG_CACHE.clear()
    glb_clips._META_CACHE.clear()
    yield


def project(tmp_path) -> ProjectLayout:
    (tmp_path / "assets" / "models").mkdir(parents=True)
    (tmp_path / "assets" / "project.toml").write_text('name = "test"\n', encoding="utf-8")
    return ProjectLayout(str(tmp_path))


def write(path: Path, data: bytes) -> str:
    """Write ``data``, a second after the previous write: the stamp caches key on mtime and
    size, and a rewrite of equal size in the same tick must still read as a change."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    _MTIME[0] += 1
    os.utime(path, ns=(_MTIME[0] * 10**9, _MTIME[0] * 10**9))
    return str(path)


def converted(layout: ProjectLayout, source: str, source_bytes: bytes, **document) -> str:
    """The converted GLB the pipeline would write for ``source``, stamped with those bytes."""
    stamp = {"paradiseSourceSha256": hashlib.sha256(source_bytes).hexdigest(),
             "paradiseConverterVersion": 1, "paradiseBlenderVersion": "Blender 5.2.1 LTS"}
    content = {"asset": {"version": "2.0", "extras": stamp}, **document}
    return write(Path(model_source.converted_path(layout, source)), glb(content))


def test_the_converted_glb_mirrors_the_source_path_under_editor_converted(tmp_path):
    layout = project(tmp_path)
    source = layout.resolve("models/props/car.blend")

    assert model_source.converted_path(layout, source) == str(
        tmp_path / ".editor" / "converted" / "models" / "props" / "car.blend.glb")


def test_a_glb_is_read_as_itself_and_a_blend_through_its_current_conversion(tmp_path):
    layout = project(tmp_path)
    model = write(tmp_path / "assets" / "models" / "crate.glb", glb({"asset": {"version": "2.0"}}))
    source = write(tmp_path / "assets" / "models" / "car.blend", b"BLENDER-v1")

    assert model_source.current_glb(model) == model
    assert model_source.current_glb(source) is None, "nothing converted yet"

    glb_path = converted(layout, source, b"BLENDER-v1")
    assert model_source.current_glb(source) == glb_path


def test_saving_the_source_makes_its_conversion_stale(tmp_path):
    layout = project(tmp_path)
    source = write(tmp_path / "assets" / "models" / "car.fbx", b"Kaydara-v1")
    glb_path = converted(layout, source, b"Kaydara-v1")
    assert model_source.is_current(source, glb_path)

    write(Path(source), b"Kaydara-v2")   # same size: only the bytes say it changed

    assert not model_source.is_current(source, glb_path)
    assert model_source.current_glb(source) is None


def test_a_glb_without_a_source_stamp_is_never_current(tmp_path):
    layout = project(tmp_path)
    source = write(tmp_path / "assets" / "models" / "car.blend", b"BLENDER-v1")
    write(Path(model_source.converted_path(layout, source)), glb({"asset": {"version": "2.0"}}))

    assert model_source.current_glb(source) is None


def test_the_converted_path_is_the_last_line_the_cli_printed():
    stdout = "converting models/car.blend\n/root/.editor/converted/models/car.blend.glb\n\n"

    assert model_source.printed_path(stdout) == "/root/.editor/converted/models/car.blend.glb"
    assert model_source.printed_path("") is None


def test_clip_settings_of_a_blend_read_its_conversion_and_land_in_its_own_sidecar(tmp_path):
    layout = project(tmp_path)
    source = write(tmp_path / "assets" / "models" / "hero.blend", b"BLENDER-hero")
    meta = tmp_path / "assets" / "models" / "hero.blend.meta"
    meta.write_text(f'schema_version = 1\nguid = "{GUID}"\n', encoding="utf-8")
    assert glb_clips.view(source) is None, "no conversion yet: no clip table to show"

    converted(layout, source, b"BLENDER-hero",
              nodes=[{"name": "root"}], skins=[{"joints": [0]}],
              animations=[{"name": "Idle"}, {"name": "Walk"}])
    view = glb_clips.view(source)
    assert [row.name for row in view.clips] == ["Idle", "Walk"] and view.identified

    glb_clips.set_root_motion(source, 1, True)
    assert glb_clips.read_settings(str(meta))[1].root_motion is True

    write(Path(source), b"BLENDER-hro2")
    with pytest.raises(glb_clips.ClipSettingsError, match="no current converted GLB"):
        glb_clips.set_root_motion(source, 0, True)
