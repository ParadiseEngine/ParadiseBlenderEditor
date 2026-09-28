"""A model that is not a ``.glb`` or ``.gltf`` is cooked from the GLB the pipeline converted it to,
and shown in a level from its source, loaded the way the converter loads it.

The clip settings read that GLB, so the one question that matters there is
whether the file under ``.editor/converted/`` is the conversion of the source as it is NOW: the
stamp inside it names the source bytes and those of every file the import read, and a saved
``.blend`` -- or an edited ``.mtl`` -- must stop matching.
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
#: Asset collection GUIDs, the identity of each model of a multi-asset .blend.
LAMP_A = "1a000000-0000-4000-8000-00000000000a"
LAMP_B = "1b000000-0000-4000-8000-00000000000b"
LAMP_C = "1c000000-0000-4000-8000-00000000000c"
RIG = "2a000000-0000-4000-8000-00000000000a"
PROP = "2b000000-0000-4000-8000-00000000000b"
GUARD = "3a000000-0000-4000-8000-00000000000a"
HERO = "3b000000-0000-4000-8000-00000000000b"

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


def converted(layout: ProjectLayout, source: str, source_bytes: bytes, dependencies=(), asset=None,
              **document) -> str:
    """The converted GLB the pipeline would write for ``source`` (or its ``asset``), stamped with
    those bytes and ``dependencies``: ``(path relative to the source's directory, bytes)`` pairs."""
    stamp = {"paradiseSourceSha256": sha256(source_bytes),
             "paradiseConverterVersion": 3, "paradiseBlenderVersion": "Blender 5.2.1 LTS",
             "paradiseDependencies": [{"path": path, "sha256": sha256(data)} for path, data in dependencies]}
    if asset is not None:
        stamp["paradiseAsset"] = asset
    content = {"asset": {"version": "2.0", "extras": stamp}, **document}
    return write(Path(model_source.converted_path(layout, source, asset)), glb(content))


def test_the_converted_glb_mirrors_the_source_path_under_editor_converted(tmp_path):
    layout = project(tmp_path)
    source = layout.resolve("models/props/car.blend")

    assert model_source.converted_path(layout, source) == str(
        tmp_path / ".editor" / "converted" / "models" / "props" / "car.blend.glb")


def test_each_asset_of_a_blend_is_converted_into_a_folder_named_after_the_source(tmp_path):
    layout = project(tmp_path)
    source = layout.resolve("models/props/lamps.blend")

    assert model_source.converted_path(layout, source, LAMP_A) == str(
        tmp_path / ".editor" / "converted" / "models" / "props" / "lamps.blend" / f"{LAMP_A}.glb")


def test_an_asset_reads_its_own_conversion_and_nothing_else(tmp_path):
    layout = project(tmp_path)
    source = write(tmp_path / "assets" / "models" / "lamps.blend", b"BLENDER-v1")
    lamp_a = converted(layout, source, b"BLENDER-v1", asset=LAMP_A)
    lamp_b = converted(layout, source, b"BLENDER-v1", asset=LAMP_B)

    assert model_source.current_glb(source, LAMP_A) == lamp_a
    assert model_source.current_glb(source, LAMP_B) == lamp_b
    assert model_source.current_glb(source) is None, "a file of assets has no whole-file model"
    assert model_source.current_glb(source, LAMP_C) is None
    # A GLB at an asset's path stamped for another asset is not that asset's conversion.
    Path(lamp_b).write_bytes(Path(lamp_a).read_bytes())
    assert model_source.current_glb(source, LAMP_B) is None


def test_saving_a_blend_makes_every_asset_conversion_stale(tmp_path):
    layout = project(tmp_path)
    source = write(tmp_path / "assets" / "models" / "lamps.blend", b"BLENDER-v1")
    for asset in (LAMP_A, LAMP_B):
        converted(layout, source, b"BLENDER-v1", asset=asset)

    write(Path(source), b"BLENDER-v2")

    assert model_source.current_glb(source, LAMP_A) is None
    assert model_source.current_glb(source, LAMP_B) is None


def test_an_asset_that_is_no_guid_or_an_asset_of_a_glb_reads_nothing(tmp_path):
    layout = project(tmp_path)
    source = write(tmp_path / "assets" / "models" / "lamps.blend", b"BLENDER-v1")
    model = write(tmp_path / "assets" / "models" / "crate.glb", glb({"asset": {"version": "2.0"}}))
    # Stamped for that asset, where the unchecked name would have led the reader.
    converted(layout, source, b"BLENDER-v1", asset="../lamps")

    for asset in ("../lamps", "Lamp_A", "", "00000000-0000-0000-0000-000000000000"):
        assert model_source.current_glb(source, asset) is None, asset
    assert model_source.current_glb(model, LAMP_A) is None


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


def test_a_placement_is_imported_as_the_converter_imports_it_and_a_blend_is_linked():
    assert set(model_source.IMPORTERS) == set(model_source.CONVERTED) - {".blend"}, \
        "a converted format has no importer to show it with, or one the converter lacks"
    assert model_source.importer_for("/a/Barrel.FBX") == model_source.Importer(
        "import_scene.fbx", (("automatic_bone_orientation", True),))
    assert model_source.importer_for("/a/set.usdz").operator == "wm.usd_import"
    assert model_source.importer_for("/a/walk.bvh").operator == "import_anim.bvh"
    for name in ("/a/crate.glb", "/a/lamp.GLTF"):
        assert model_source.importer_for(name) == model_source.GLTF_IMPORTER, name
        assert not model_source.is_linked(name), name
    assert model_source.importer_for("/a/crate.blend") is None
    assert model_source.is_linked("/a/Crate.BLEND")
    assert model_source.importer_for("/a/crate.dae") is None


def test_an_import_depends_on_the_obj_material_libraries_and_gltf_buffers(tmp_path):
    models = tmp_path / "assets" / "models"
    source = write(models / "props" / "tree.obj",
                   b"# tree\nmtllib ../shared/tree.mtl\nmtllib bark.mtl\nv 0 0 0\n")
    assert model_source.native_dependencies(source) == [
        str(models / "shared" / "tree.mtl"), str(models / "props" / "bark.mtl")]

    lamp = models / "lamp.gltf"
    lamp.write_text('{"asset": {"version": "2.0"}, "buffers": [{"uri": "lamp.bin", "byteLength": 0}]}',
                    encoding="utf-8")
    assert model_source.native_dependencies(str(lamp)) == [str(models / "lamp.bin")]

    assert model_source.native_dependencies(write(models / "barrel.fbx", b"Kaydara")) == []
    assert model_source.native_dependencies(str(models / "gone.obj")) == []


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


def test_the_edit_source_refusal_names_the_format():
    obj = model_source.edit_source_refusal("/a/crate.obj")
    assert "crate.obj is an OBJ file" in obj and "export the OBJ again" in obj
    assert "a USDZ file" in model_source.edit_source_refusal("/a/set.usdz")
    assert "a GLB file" in model_source.edit_source_refusal("/a/crate.glb")


def test_a_conversion_with_no_mesh_is_skeleton_only(tmp_path):
    layout = project(tmp_path)
    walk = write(tmp_path / "assets" / "models" / "walk.bvh", b"HIERARCHY")
    crate = write(tmp_path / "assets" / "models" / "crate.obj", b"v 0 0 0")
    # A .bvh is animation only by its format; any other source is unknown until converted.
    assert model_source.is_skeleton_only(walk)
    assert not model_source.is_skeleton_only(crate), "nothing converted yet: nothing is known"

    converted(layout, walk, b"HIERARCHY", nodes=[{"name": "Hips"}], skins=[{"joints": [0]}],
              animations=[{"name": "Walk"}])
    converted(layout, crate, b"v 0 0 0", meshes=[{"primitives": []}])

    assert model_source.is_skeleton_only(walk)
    assert not model_source.is_skeleton_only(crate)


def test_an_asset_of_skeleton_and_clips_alone_is_skeleton_only(tmp_path):
    layout = project(tmp_path)
    source = write(tmp_path / "assets" / "models" / "cast.blend", b"BLENDER-cast")
    converted(layout, source, b"BLENDER-cast", asset=RIG, nodes=[{"name": "Hips"}],
              skins=[{"joints": [0]}], animations=[{"name": "Walk"}])
    converted(layout, source, b"BLENDER-cast", asset=PROP, meshes=[{"primitives": []}])

    assert model_source.is_skeleton_only(source, RIG)
    assert not model_source.is_skeleton_only(source, PROP)
    refusal = model_source.no_mesh_refusal(model_source.Model(source, RIG, "Rig"))
    assert "Rig in cast.blend holds no mesh" in refusal


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


def test_clip_settings_of_each_asset_are_keyed_by_the_asset_in_the_one_sidecar(tmp_path):
    layout = project(tmp_path)
    source = write(tmp_path / "assets" / "models" / "cast.blend", b"BLENDER-cast")
    meta = tmp_path / "assets" / "models" / "cast.blend.meta"
    meta.write_text(
        f'schema_version = 1\nguid = "{GUID}"\n\n[glb]\n'
        'clips = [ { index = 0, name = "Stale", root_motion = true } ]\n', encoding="utf-8")
    rig = {"nodes": [{"name": "root"}], "skins": [{"joints": [0]}]}
    converted(layout, source, b"BLENDER-cast", asset=HERO, animations=[{"name": "Idle"}, {"name": "Run"}],
              **rig)
    converted(layout, source, b"BLENDER-cast", asset=GUARD, animations=[{"name": "Patrol"}], **rig)

    glb_clips.set_root_motion(source, 1, True, HERO)
    glb_clips.set_root_bone(source, 0, "root", GUARD)

    assert glb_clips.read_settings(str(meta), HERO)[1].root_motion is True
    assert glb_clips.read_settings(str(meta), GUARD)[0].root_bone == "root"
    assert glb_clips.read_settings(str(meta))[0].name == "Stale", "another model's entry was dropped"
    assert set(glb_clips.read_settings(str(meta), HERO)) == {1}
    assert [row.setting.root_motion for row in glb_clips.view(source, HERO).clips] == [False, True]
    assert [row.setting.root_bone for row in glb_clips.view(source, GUARD).clips] == ["root"]
    text = meta.read_text(encoding="utf-8")
    # The whole-file entry first, then each asset's by GUID in ordinal order, each by index.
    positions = [text.index(entry) for entry in ('name = "Stale"', f'asset = "{GUARD}"', f'asset = "{HERO}"')]
    assert positions == sorted(positions), text
    assert f'asset = "{HERO}", index = 1, name = "Run", root_motion = true' in text, text

    glb_clips.set_root_motion(source, 1, False, HERO)
    assert glb_clips.read_settings(str(meta), HERO) == {}
    assert f'asset = "{HERO}"' not in meta.read_text(encoding="utf-8")
