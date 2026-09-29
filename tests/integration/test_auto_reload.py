"""Dependency refresh and unsaved-edit protection in real Blender, using a disposable project."""

from __future__ import annotations

import contextlib
import io
import json
import struct
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import addon_utils
import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_overrides import CHILD_LOCAL, INSTANCE, LEVEL_GUID, ROOT_LOCAL, TAG, make_project, opened

from paradise_assets import action_ops, edits
from paradise_assets.document import prefab
from paradise_assets.document.asset_reference import AssetReference
from paradise_assets.materialize import light_preview, load, meshes, refresh, save, shapes, store

MESH = "cccccccc-0000-4000-8000-000000000001"
ASSET = "dddddddd-0000-4000-8000-000000000001"
LIGHT = "eeeeeeee-0000-4000-8000-000000000001"
COLLIDER = "eeeeeeee-0000-4000-8000-000000000002"
SCHEMA = {"components": [
    {"id": LIGHT, "type": "Test.Omni", "previewLight": "Point", "fields": []},
    {"id": COLLIDER, "type": "Game.AuthoredColliders", "fields": [{
        "name": "Shapes", "type": "array", "items": {
            "name": "Shapes", "type": "object", "authoredBy": "shape", "fields": [
                {"name": "ShapeType", "type": "enum", "values": ["Box", "Sphere", "Capsule"]},
                {"name": "LocalCenter", "type": "vector3"},
                {"name": "LocalRotation", "type": "quaternion"},
                {"name": "Size", "type": "vector3"},
                {"name": "Radius", "type": "float"},
                {"name": "Height", "type": "float"},
            ]}}]},
]}
SPHERE = {"ShapeType": "Sphere", "LocalCenter": [0.0, 0.0, 0.0], "LocalRotation": [0.0, 0.0, 0.0, 1.0],
          "Size": [0.0, 0.0, 0.0], "Radius": 0.5, "Height": 0.0}
_clock = 0.0


def settle():
    global _clock
    _clock += 2.0
    refresh.tick(now=_clock)
    refresh.tick(now=_clock + 0.5)


def instance():
    return store.object_with_guid(bpy.context.scene, INSTANCE)


def value():
    return next(component for component in store.component_json(instance())
                if component["id"] == TAG)["data"]["Value"]


def change_prefab(path: str, number: int):
    document = prefab.loads(Path(path).read_text(), path)
    document.by_guid()[ROOT_LOCAL].component(TAG).data["Value"] = number
    Path(path).write_text(prefab.dumps(document))


def triangle(scale: float) -> bytes:
    return struct.pack("<9f", 0, 0, 0, scale, 0, 0, 0, 1, 0)


def gltf_document():
    return {"asset": {"version": "2.0"}, "scene": 0,
            "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
            "buffers": [{"byteLength": 36}],
            "bufferViews": [{"buffer": 0, "byteLength": 36}],
            "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
                           "min": [0, 0, 0], "max": [10, 1, 0]}]}


def write_glb(path: Path, scale: float):
    document = json.dumps(gltf_document()).encode()
    document += b" " * (-len(document) % 4)
    binary = triangle(scale)
    path.write_bytes(struct.pack("<III", 0x46546C67, 2, 28 + len(document) + len(binary))
                     + struct.pack("<II", len(document), 0x4E4F534A) + document
                     + struct.pack("<II", len(binary), 0x004E4942) + binary)


def write_blend(path: Path, scale: float):
    collection = bpy.data.collections.new("ReloadAsset")
    collection.asset_mark()
    collection["paradise_guid"] = ASSET
    mesh = bpy.data.meshes.new("SourceTriangle")
    mesh.from_pydata([(0, 0, 0), (scale, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    obj = bpy.data.objects.new("SourceTriangle", mesh)
    collection.objects.link(obj)
    bpy.data.libraries.write(str(path), {collection})
    bpy.data.collections.remove(collection)
    bpy.data.objects.remove(obj)
    bpy.data.meshes.remove(mesh)


def mesh_document(path: Path, source: str, *, asset: bool = False):
    path.write_text('schema_version = 1\nslot = "mesh"\n'
                    f'source = {{ guid = "{MESH}", path = "models/{source}" }}\n'
                    + (f'asset = {{ guid = "{ASSET}", name = "ReloadAsset" }}\n' if asset else ""))


def model_width():
    collection = instance().instance_collection
    return max(vertex.co.x for obj in collection.all_objects if obj.type == "MESH"
               for vertex in obj.data.vertices)


def check_prefab_updates(work: str):
    level, nested = make_project(work)
    opened(level)
    scene = bpy.context.scene
    extra = bpy.data.objects.new("AuthorNote", None)
    scene.collection.objects.link(extra)
    camera = bpy.data.objects.new("AuthorCamera", bpy.data.cameras.new("AuthorCamera"))
    scene.collection.objects.link(camera)
    scene.camera = camera
    selected = instance()
    camera.parent = selected
    camera.location = (1.0, 2.0, 3.0)
    selected.select_set(True)
    extra.select_set(True)
    scene.view_layers[0].objects.active = selected
    original = Path(level).read_bytes()

    change_prefab(nested, 2)
    settle()
    assert value() == 2
    assert Path(level).read_bytes() == original, "auto-reload wrote the level document"
    assert extra.name in scene.objects and scene.camera == camera
    assert camera.parent == instance() and tuple(camera.location) == (1.0, 2.0, 3.0)
    assert instance().select_get() and extra.select_get()
    assert scene.view_layers[0].objects.active == instance()
    print("PASS nested prefab updates preserve the level document, extras, camera and selection")

    before = instance().as_pointer()
    Path(work, "assets", "unrelated.prefab").write_text("unrelated")
    settle()
    assert instance().as_pointer() == before, "an unrelated file replaced live level objects"

    with patch.object(action_ops, "after_load", wraps=action_ops.after_load) as restored:
        save.save_prefab(scene, invoke_actions=False)
        settle()
        assert restored.call_count == 0, "saving invoked authored actions again during reload"
    print("PASS a self-written save does not replay authored actions during refresh")

    instance().location.x = 9.0
    change_prefab(nested, 3)
    settle()
    assert value() == 2 and instance().location.x == 9.0
    assert refresh.pending_reason(scene) is not None
    save.save_prefab(scene, invoke_actions=False)
    settle()
    assert value() == 3 and instance().location.x == 9.0
    assert refresh.pending_reason(scene) is None
    written = prefab.loads(Path(level).read_text(), level).by_guid()[INSTANCE]
    assert written.component("7e55c210-3d41-4b8a-8f26-9c0a5e71b4d2").data["Position"][0] == 9.0
    print("PASS unsaved placement pauses reload, then save preserves it and resumes refresh")

    edits.set_field(instance(), TAG, "Colour", "green")
    change_prefab(nested, 4)
    settle()
    assert value() == 3 and edits.count(instance()) == 1
    edits.clear(instance())
    settle()
    assert value() == 4 and refresh.pending_reason(scene) is None
    print("PASS pending component edits survive, and cancelling them resumes refresh")

    child = next(obj for obj in scene.collection.all_objects
                 if (address := store.local_of(obj)) is not None and address[1] == CHILD_LOCAL)
    bpy.data.objects.remove(child, do_unlink=True)
    change_prefab(nested, 5)
    settle()
    assert value() == 4 and refresh.pending_reason(scene) is not None
    assert not any(store.is_derived(obj) for obj in scene.collection.all_objects)
    save.save_prefab(scene, invoke_actions=False)
    settle()
    assert value() == 5
    assert not any(store.is_derived(obj) for obj in scene.collection.all_objects)
    print("PASS unsaved child deletion is not resurrected and survives the subsequent save")

    before = instance().as_pointer()
    valid = Path(nested).read_text()
    Path(nested).write_text("schema_version = 1\nbroken [[")
    settle()
    assert instance().as_pointer() == before and value() == 5
    assert refresh.pending_reason(scene) is not None
    Path(nested).write_text(valid)
    change_prefab(nested, 6)
    settle()
    assert value() == 6 and refresh.pending_reason(scene) is None
    assert bpy.app.timers.is_registered(refresh.tick)
    print("PASS an incomplete prefab save keeps the last valid scene until repaired")

    with patch.object(action_ops, "busy", return_value=True):
        change_prefab(nested, 7)
        settle()
        assert value() == 6 and refresh.pending_reason(scene) is not None
    settle()
    assert value() == 7 and refresh.pending_reason(scene) is None
    print("PASS an active authored action defers automatic reload until it finishes")

    document = prefab.loads(Path(level).read_text(), level)
    document.by_guid()[INSTANCE].meta.data["Name"] = "RenamedElsewhere"
    Path(level).write_text(prefab.dumps(document))
    assert store.read_state(scene).is_stale
    settle()
    assert store.document_name(instance()) == "RenamedElsewhere" and not store.read_state(scene).is_stale, (
        instance().name, store.read_state(scene).is_stale, refresh.pending_reason(scene))
    print("PASS editing the level document externally refreshes its clean Blender view")

    added = Path(work, "assets", "prefabs", "added.prefab")
    reference = document.by_guid()[INSTANCE].prefab
    document.by_guid()[INSTANCE].prefab = AssetReference(reference.guid, "prefabs/added.prefab")
    Path(level).write_text(prefab.dumps(document))
    before = instance().as_pointer()
    settle()
    assert instance().as_pointer() == before and refresh.pending_reason(scene) is not None
    added.write_text(Path(nested).read_text())
    change_prefab(str(added), 8)
    settle()
    assert value() == 8 and refresh.pending_reason(scene) is None
    print("PASS a newly referenced missing prefab preserves the scene and reloads when created")

    document.objects = [document.root()]
    Path(level).write_text(prefab.dumps(document))
    added.unlink()
    settle()
    assert instance() is None and refresh.pending_reason(scene) is None
    assert not store.read_state(scene).is_stale
    print("PASS a deleted dependency no longer referenced does not block reload")


def check_model_updates(work: str):
    level, nested = make_project(work)
    models = Path(work, "assets", "models")
    models.mkdir()
    write_glb(models / "shape.glb", 1.0)
    mesh_document(models / "shape.mesh", "shape.glb")
    document = prefab.loads(Path(nested).read_text(), nested)
    document.by_guid()[ROOT_LOCAL].components.append(prefab.PrefabComponent(
        MESH, "Game.StaticMesh", {"Mesh": {"guid": MESH, "path": "models/shape.mesh"}}))
    Path(nested).write_text(prefab.dumps(document))
    layout = opened(level)
    original = Path(level).read_bytes()
    assert model_width() == 1.0
    scene = bpy.context.scene
    collection = instance().instance_collection
    extra = bpy.data.objects.new("ExtraPlacement", None)
    extra.instance_type = "COLLECTION"
    extra.instance_collection = collection
    scene.collection.objects.link(extra)
    shared = bpy.data.scenes.new("AnotherLevelView")
    with bpy.context.temp_override(scene=shared, view_layer=shared.view_layers[0]):
        load.load_document(shared, prefab.loads(Path(level).read_text(), level), level, layout)
    local_placement = store.object_with_guid(shared, INSTANCE)
    local_placement.location.x = 23.0
    write_glb(models / "shape.glb", 3.0)
    settle()
    assert model_width() == 3.0
    assert extra.instance_collection == collection == instance().instance_collection
    assert local_placement.instance_collection == collection and local_placement.location.x == 23.0
    assert refresh.pending_reason(shared) is not None
    bpy.data.scenes.remove(shared)
    print("PASS changed GLB geometry preserves shared instances and another scene's unsaved edits")

    before = instance().as_pointer()
    (models / "shape.glb").write_bytes(b"incomplete")
    settle()
    assert instance().as_pointer() == before and model_width() == 3.0
    assert refresh.pending_reason(scene) is not None
    write_glb(models / "shape.glb", 6.0)
    settle()
    assert model_width() == 6.0 and refresh.pending_reason(scene) is None
    print("PASS an incomplete model save keeps the last valid scene and recovers after repair")

    import_model = meshes.MeshLibrary._import

    def saved_during_import(library, path, name, existing):
        collection = import_model(library, path, name, existing)
        write_glb(models / "shape.glb", 8.0)
        return collection

    write_glb(models / "shape.glb", 7.0)
    with patch.object(meshes.MeshLibrary, "_import", saved_during_import):
        settle()
        assert model_width() == 7.0
    settle()
    assert model_width() == 8.0
    print("PASS a model saved during import is re-imported on the next settled refresh")

    gltf = gltf_document()
    gltf["buffers"][0]["uri"] = "shape.bin"
    (models / "shape.gltf").write_text(json.dumps(gltf))
    (models / "shape.bin").write_bytes(triangle(2.0))
    mesh_document(models / "shape.mesh", "shape.gltf")
    settle()
    assert model_width() == 2.0
    source_stamp = store.stamp_of(str(models / "shape.gltf"))
    (models / "shape.bin").write_bytes(triangle(4.0))
    settle()
    assert store.stamp_of(str(models / "shape.gltf")) == source_stamp and model_width() == 4.0
    print("PASS a mesh document retargets the source, and a glTF buffer-only edit refreshes geometry")

    write_blend(models / "shape.blend", 2.0)
    mesh_document(models / "shape.mesh", "shape.blend", asset=True)
    settle()
    assert model_width() == 2.0
    write_blend(models / "shape.blend", 5.0)
    settle()
    assert model_width() == 5.0
    assert any(obj.library is not None for obj in instance().instance_collection.all_objects)
    assert Path(level).read_bytes() == original
    print("PASS a linked Blender asset collection reloads without writing the level")


def check_read_stamps(work: str):
    level, nested = make_project(work)
    opened(level)
    change_prefab(nested, 2)
    with patch.object(load.light_preview, "refresh", side_effect=lambda _scene: change_prefab(nested, 3)):
        settle()
        assert value() == 2
    settle()
    assert value() == 3
    print("PASS a prefab saved during materialization is not recorded as already displayed")

    valid = Path(level).read_text()
    Path(level).write_text("broken [[")
    settle()
    assert value() == 3 and bpy.app.timers.is_registered(refresh.tick)
    Path(level).write_text(valid)
    settle()
    assert value() == 3 and refresh.pending_reason(bpy.context.scene) is None
    print("PASS malformed root documents do not stop the registered refresh timer")

    refresh._before_load()
    refresh.deferred(bpy.context.scene)
    edits.set_field(instance(), TAG, "Colour", "green")
    change_prefab(nested, 4)
    settle()
    assert value() == 3 and refresh.pending_reason(bpy.context.scene) is not None
    save.save_prefab(bpy.context.scene, invoke_actions=False)
    settle()
    assert value() == 4 and refresh.pending_reason(bpy.context.scene) is None
    print("PASS a deferred workfile refresh resumes after its local edits are saved")

    instance().location.x = 31.0
    refresh._after_undo()
    settle()
    assert instance().location.x == 31.0 and refresh.pending_reason(bpy.context.scene) is not None
    save.save_prefab(bpy.context.scene, invoke_actions=False)
    settle()
    assert instance().location.x == 31.0 and refresh.pending_reason(bpy.context.scene) is None
    print("PASS undo/redo invalidation keeps local edits until saved, then refresh resumes")


def check_generated_children(work: str):
    level, nested = make_project(work)
    Path(work, ".editor").mkdir()
    Path(work, ".editor", "authoring-schema.json").write_text(json.dumps(SCHEMA))
    document = prefab.loads(Path(level).read_text(), level)
    document.by_guid()[LEVEL_GUID].components += [
        prefab.PrefabComponent(LIGHT, "Test.Omni", {}),
        prefab.PrefabComponent(COLLIDER, "Game.AuthoredColliders", {"Shapes": [SPHERE]}),
    ]
    Path(level).write_text(prefab.dumps(document))
    opened(level)
    scene = bpy.context.scene

    def root():
        return store.object_with_guid(scene, LEVEL_GUID)

    def previews():
        return [obj for obj in scene.objects if light_preview.is_preview(obj)]

    assert [preview.parent for preview in previews()] == [root()]
    note = bpy.data.objects.new("AuthorChild", None)
    scene.collection.objects.link(note)
    note.parent = root()
    note.location = (1.0, 2.0, 3.0)
    note.select_set(True)
    change_prefab(nested, 2)
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        settle()
    assert value() == 2 and "deferred" not in printed.getvalue(), printed.getvalue()
    assert note.parent == root() and tuple(note.location) == (1.0, 2.0, 3.0) and note.select_get()
    assert [preview.parent for preview in previews()] == [root()]
    print("PASS light previews are rebuilt, not restored, beside a re-parented author child")

    shape = next(obj for obj in scene.objects if shapes.is_shape(obj))
    shape.empty_display_size = 1.25
    change_prefab(nested, 3)
    settle()
    assert value() == 2 and shape.empty_display_size == 1.25
    assert refresh.pending_reason(scene) is not None
    save.save_prefab(scene, invoke_actions=False)
    settle()
    assert value() == 3 and refresh.pending_reason(scene) is None
    saved = prefab.loads(Path(level).read_text(), level).by_guid()[LEVEL_GUID]
    assert saved.component(COLLIDER).data["Shapes"][0]["Radius"] == 1.25
    assert next(obj for obj in scene.objects if shapes.is_shape(obj)).empty_display_size == 1.25
    print("PASS resizing a collision shape pauses reload until saved, and the save keeps it")


def main() -> int:
    addon_utils.enable("paradise_assets", default_set=True, persistent=False)
    bpy.context.preferences.addons["paradise_assets"].preferences.auto_watch = False
    with tempfile.TemporaryDirectory(prefix="paradise-auto-reload-") as work:
        refresh.unregister_handler()
        assert not bpy.app.timers.is_registered(refresh.tick)
        assert refresh._after_undo not in bpy.app.handlers.undo_post
        assert refresh._after_undo not in bpy.app.handlers.redo_post
        refresh.register_handler()
        refresh.register_handler()
        assert bpy.app.timers.is_registered(refresh.tick)
        assert bpy.app.handlers.load_pre.count(refresh._before_load) == 1
        assert bpy.app.handlers.undo_post.count(refresh._after_undo) == 1
        assert bpy.app.handlers.redo_post.count(refresh._after_undo) == 1
        check_prefab_updates(str(Path(work, "prefabs")))
        check_model_updates(str(Path(work, "models")))
        check_read_stamps(str(Path(work, "read-stamps")))
        check_generated_children(str(Path(work, "generated")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
