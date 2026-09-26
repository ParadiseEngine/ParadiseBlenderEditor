"""A model that is not a ``.glb`` or ``.gltf`` is read through the GLB the pipeline converted it to.

The viewport, Make Mesh Editable and the clip settings all read that GLB, so the one question
that matters is whether the file under ``.editor/converted/`` is the conversion of the source as
it is NOW: the stamp inside it names the source bytes and those of every file the import read,
and a saved ``.blend`` -- or an edited ``.mtl`` -- must stop matching.
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


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def converted(layout: ProjectLayout, source: str, source_bytes: bytes, dependencies=(), **document) -> str:
    """The converted GLB the pipeline would write for ``source``, stamped with those bytes and
    ``dependencies``: ``(path relative to the source's directory, bytes)`` pairs."""
    stamp = {"paradiseSourceSha256": sha256(source_bytes),
             "paradiseConverterVersion": 2, "paradiseBlenderVersion": "Blender 5.2.1 LTS",
             "paradiseDependencies": [{"path": path, "sha256": sha256(data)} for path, data in dependencies]}
    content = {"asset": {"version": "2.0", "extras": stamp}, **document}
    return write(Path(model_source.converted_path(layout, source)), glb(content))


def test_the_converted_glb_mirrors_the_source_path_under_editor_converted(tmp_path):
    layout = project(tmp_path)
    source = layout.resolve("models/props/car.blend")

    assert model_source.converted_path(layout, source) == str(
        tmp_path / ".editor" / "converted" / "models" / "props" / "car.blend.glb")


def test_a_glb_or_gltf_is_read_as_itself_and_a_blend_through_its_current_conversion(tmp_path):
    layout = project(tmp_path)
    model = write(tmp_path / "assets" / "models" / "crate.glb", glb({"asset": {"version": "2.0"}}))
    text = write(tmp_path / "assets" / "models" / "tree.gltf", b'{"asset": {"version": "2.0"}}')
    source = write(tmp_path / "assets" / "models" / "car.blend", b"BLENDER-v1")

    assert model_source.current_glb(model) == model
    assert model_source.current_glb(text) == text
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


def test_every_format_blender_imports_is_a_converted_model_and_gltf_is_read_directly():
    for name in ("a.blend", "a.fbx", "a.obj", "a.ply", "a.stl", "a.usd", "a.usda",
                 "a.usdc", "a.usdz", "a.abc", "a.bvh", "A.OBJ"):
        assert model_source.is_model(name) and model_source.is_converted(name), name
    for name in ("a.glb", "a.gltf", "A.GLTF"):
        assert model_source.is_model(name) and not model_source.is_converted(name), name
    for name in ("a.dae", "a.svg", "a.png", "a.mesh", "a.glb.meta"):
        assert not model_source.is_model(name), name


def test_editing_a_file_the_import_read_makes_the_conversion_stale(tmp_path):
    layout = project(tmp_path)
    models = tmp_path / "assets" / "models"
    source = write(models / "crate.obj", b"mtllib crate.mtl\n")
    mtl = write(models / "crate.mtl", b"map_Kd textures/wood.png\n")
    write(models / "textures" / "wood.png", b"PNG-oak")
    glb_path = converted(layout, source, b"mtllib crate.mtl\n",
                         dependencies=[("crate.mtl", b"map_Kd textures/wood.png\n"),
                                       ("textures/wood.png", b"PNG-oak")])
    assert model_source.current_glb(source) == glb_path

    write(models / "textures" / "wood.png", b"PNG-elm")   # same size: only the bytes differ
    assert model_source.current_glb(source) is None, "a changed texture left the conversion current"

    write(models / "textures" / "wood.png", b"PNG-oak")
    assert model_source.current_glb(source) == glb_path

    os.unlink(mtl)
    assert model_source.current_glb(source) is None, "a dependency that is gone left it current"


def test_dependencies_resolve_against_the_source_directory_not_the_project(tmp_path):
    layout = project(tmp_path)
    source = write(tmp_path / "assets" / "models" / "props" / "tree.obj", b"mtllib ../shared/tree.mtl\n")
    write(tmp_path / "assets" / "models" / "shared" / "tree.mtl", b"MTL")
    write(tmp_path / "assets" / "tree.mtl", b"OTHER")
    glb_path = converted(layout, source, b"mtllib ../shared/tree.mtl\n",
                         dependencies=[("../shared/tree.mtl", b"MTL")])

    assert model_source.is_current(source, glb_path)


def test_a_conversion_that_records_no_dependency_list_is_never_current(tmp_path):
    layout = project(tmp_path)
    source = write(tmp_path / "assets" / "models" / "car.fbx", b"Kaydara")
    stamp = {"paradiseSourceSha256": sha256(b"Kaydara"), "paradiseConverterVersion": 1}
    write(Path(model_source.converted_path(layout, source)),
          glb({"asset": {"version": "2.0", "extras": stamp}}))

    assert model_source.current_glb(source) is None


def test_the_interchange_refusal_names_the_format_and_a_blend_is_edited_at_source():
    assert "Edit Source in New Blender" in model_source.edit_in_place_refusal("/a/crate.blend")
    obj = model_source.edit_in_place_refusal("/a/crate.obj")
    assert "crate.obj is an OBJ file" in obj and "export the OBJ again" in obj
    assert "a USDZ file" in model_source.edit_in_place_refusal("/a/set.usdz")


def test_a_conversion_with_no_mesh_is_skeleton_only(tmp_path):
    layout = project(tmp_path)
    walk = write(tmp_path / "assets" / "models" / "walk.bvh", b"HIERARCHY")
    crate = write(tmp_path / "assets" / "models" / "crate.obj", b"v 0 0 0")
    assert not model_source.is_skeleton_only(walk), "nothing converted yet: nothing is known"

    converted(layout, walk, b"HIERARCHY", nodes=[{"name": "Hips"}], skins=[{"joints": [0]}],
              animations=[{"name": "Walk"}])
    converted(layout, crate, b"v 0 0 0", meshes=[{"primitives": []}])

    assert model_source.is_skeleton_only(walk)
    assert not model_source.is_skeleton_only(crate)


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
