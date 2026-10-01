"""Source-filename collection labels, including unchanged cached models.

No project, addon registration or Paradise CLI is needed. Run from the repository root:
  blender --background --factory-startup --python-exit-code 1 \
    --python tests/integration/test_model_collection_names.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paradise_assets.document import asset_guids, model_source
from paradise_assets.materialize import meshes

ASSETS = (
    ("Tall", "11111111-1111-4111-8111-111111111111"),
    ("Wide", "22222222-2222-4222-8222-222222222222"),
)


def make_sources(root: Path) -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add(size=1)
    cube = bpy.context.object
    cube.name = "Crate"
    bpy.data.libraries.write(str(root / "Crate.blend"), {bpy.context.scene})
    for extension, format_name in (("glb", "GLB"), ("gltf", "GLTF_SEPARATE")):
        bpy.ops.export_scene.gltf(filepath=str(root / f"Crate.{extension}"),
                                 export_format=format_name)
    (root / "Crate.obj").write_text(
        "o Crate\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")

    upper = root / "upper"
    upper.mkdir()
    for extension in ("blend", "glb", "gltf", "obj"):
        shutil.copyfile(root / f"Crate.{extension}", upper / f"Crate.{extension.upper()}")
    for buffer in root.glob("*.bin"):
        shutil.copyfile(buffer, upper / buffer.name)

    assets = set()
    for name, guid in ASSETS:
        collection = bpy.data.collections.new(name)
        collection.asset_mark()
        collection[asset_guids.PROPERTY] = guid
        obj = cube.copy()
        obj.name = name
        collection.objects.link(obj)
        assets.add(collection)
    multi = root / "multi"
    multi.mkdir()
    bpy.data.libraries.write(str(multi / "Crate.blend"), assets)
    for directory in ("left", "right"):
        target = root / directory
        target.mkdir()
        shutil.copyfile(root / "Crate.obj", target / "Crate.obj")
    bpy.ops.wm.read_factory_settings(use_empty=True)


def load(model: model_source.Model) -> bpy.types.Collection:
    warnings = []
    collection = meshes.MeshLibrary(bpy.context.scene, warnings.append).collection_for(model)
    assert collection is not None, (model, warnings)
    assert not warnings, warnings
    assert any(obj.type == "MESH" for obj in collection.all_objects), model
    assert meshes.model_of(collection) == model
    return collection


def assert_cached_name(model: model_source.Model, collection: bpy.types.Collection,
                       expected: str) -> None:
    """A fresh loader must migrate only the label, not refresh unchanged geometry."""
    placements = []
    for index in range(2):
        instance = bpy.data.objects.new(f"Placement{index}", None)
        instance.instance_type = "COLLECTION"
        instance.instance_collection = collection
        bpy.context.scene.collection.objects.link(instance)
        placements.append(instance)

    pointer = collection.as_pointer()
    metadata = dict(collection.items())
    children = set(bpy.data.collections[meshes.LIBRARY_COLLECTION].children)
    objects = list(collection.all_objects)
    geometry = {obj.as_pointer(): obj.data.as_pointer() for obj in objects if obj.type == "MESH"}
    linked = {obj.library: dict(obj.library.items()) for obj in objects if obj.library is not None}
    paths = [model.path, *collection[meshes.DEPENDENCIES_KEY].splitlines()]
    source_state = {
        path: (Path(path).stat().st_mtime_ns, Path(path).read_bytes()) for path in paths
    }
    warnings = []
    fresh = meshes.MeshLibrary(bpy.context.scene, warnings.append)
    # Re-linking can reuse linked IDs too: explicitly forbid the content-refresh paths.
    with patch.object(meshes, "_empty", side_effect=AssertionError("cache contents rebuilt")), \
            patch.object(meshes, "_stamp_library", side_effect=AssertionError("library re-read")):
        reused = fresh.collection_for(model)
    assert not warnings, warnings
    assert reused == collection and reused.as_pointer() == pointer
    assert reused.name == expected, (reused.name, expected)
    assert dict(reused.items()) == metadata, "source/asset/stamp/dependency metadata changed"
    assert {obj.as_pointer() for obj in reused.all_objects} == {obj.as_pointer() for obj in objects}
    assert {obj.as_pointer(): obj.data.as_pointer() for obj in reused.all_objects
            if obj.type == "MESH"} == geometry
    assert all(dict(library.items()) == tags for library, tags in linked.items())
    assert all(instance.instance_collection == reused for instance in placements)
    assert set(fresh.sources) == set(paths)
    assert source_state == {
        path: (Path(path).stat().st_mtime_ns, Path(path).read_bytes()) for path in paths
    }
    assert set(bpy.data.collections[meshes.LIBRARY_COLLECTION].children) == children


def test_format_names(root: Path) -> None:
    for directory, suffixes in ((root, ("blend", "gltf", "glb", "obj")),
                                (root / "upper", ("BLEND", "GLTF", "GLB", "OBJ"))):
        for suffix in suffixes:
            bpy.ops.wm.read_factory_settings(use_empty=True)
            model = model_source.Model(str(directory / f"Crate.{suffix}"))
            collection = load(model)
            assert collection.name == f"Crate.{suffix}", collection.name
            mesh = next(obj for obj in collection.all_objects if obj.type == "MESH")
            if suffix.lower() == "blend":
                assert mesh.library is not None and mesh.data.library is not None
                assert Path(bpy.path.abspath(mesh.library.filepath)).resolve() == Path(model.path).resolve()
            else:
                assert mesh.library is None and mesh.data.library is None


def test_legacy_cache_names(root: Path) -> None:
    for suffix in ("blend", "gltf", "obj", "glb"):
        expected = f"Crate.{suffix}"
        bpy.ops.wm.read_factory_settings(use_empty=True)
        model = model_source.Model(str(root / f"Crate.{suffix}"))
        collection = load(model)
        assert collection[meshes.SOURCE_KEY] == model.path
        assert collection[meshes.STAMP_KEY]
        if suffix == "gltf":
            assert collection[meshes.DEPENDENCIES_KEY], "fixture must exercise an external buffer"
        collection.name = "GLB/Crate" if suffix == "glb" else f"GLB/Crate.{suffix}"
        assert_cached_name(model, collection, expected)
        assert_cached_name(model, collection, expected)


def test_asset_names(root: Path) -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    path = str(root / "multi" / "Crate.blend")
    collections = []
    for name, guid in ASSETS:
        model = model_source.Model(path, guid, name)
        collection = load(model)
        assert collection.name == f"Crate.blend/{name}"
        assert collection[meshes.ASSET_KEY] == guid
        assert collection[meshes.ASSET_NAME_KEY] == name
        assert all(obj.library is not None for obj in collection.all_objects)
        collection.name = f"GLB/Crate.blend/{name}"
        assert_cached_name(model, collection, f"Crate.blend/{name}")
        collections.append(collection)
    assert collections[0] != collections[1]
    for (name, guid), collection in zip(ASSETS, collections, strict=True):
        for hint in (None, ""):
            assert_cached_name(model_source.Model(path, guid, hint), collection,
                               f"Crate.blend/{guid}")
            assert meshes.model_of(collection).name == name
        assert_cached_name(model_source.Model(path, guid, name), collection,
                           f"Crate.blend/{name}")


def test_same_basename_identity(root: Path) -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    models = [model_source.Model(str(root / directory / "Crate.obj"))
              for directory in ("left", "right")]
    collections = [load(model) for model in models]
    assert collections[0] != collections[1]
    assert collections[0].name != collections[1].name
    assert all(collection.name.startswith("Crate.obj") for collection in collections)
    assert set(collections[0].all_objects).isdisjoint(collections[1].all_objects)
    placements = []
    for model, collection in zip(models, collections, strict=True):
        instance = bpy.data.objects.new("Crate placement", None)
        instance.instance_type = "COLLECTION"
        instance.instance_collection = collection
        bpy.context.scene.collection.objects.link(instance)
        placements.append(instance)
        assert collection[meshes.SOURCE_KEY] == model.path
        collection.name = "GLB/Crate.obj"
    fresh = meshes.MeshLibrary(bpy.context.scene)
    for model, collection, instance in reversed(list(zip(models, collections, placements, strict=True))):
        assert fresh.collection_for(model) == collection
        assert meshes.model_of(collection) == model
        assert instance.instance_collection == collection
        assert collection.name.startswith("Crate.obj")
    assert collections[0].name != collections[1].name
    assert fresh.imported == 2
    assert fresh.sources == {model.path for model in models}
    assert set(bpy.data.collections[meshes.LIBRARY_COLLECTION].children) == set(collections)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="paradise-collection-names-") as temporary:
        root = Path(temporary)
        make_sources(root)
        for test in (test_format_names, test_legacy_cache_names,
                     test_asset_names, test_same_basename_identity):
            test(root)
            print(f"PASS: {test.__name__}")
        bpy.ops.wm.read_factory_settings(use_empty=True)
    print("PASS: model collection names (no project or CLI)")


if __name__ == "__main__":
    main()
