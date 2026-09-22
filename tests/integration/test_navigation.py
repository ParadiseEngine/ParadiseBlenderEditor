"""Navigation controls and real Recast baking in an isolated disposable asset project.

Set PARADISE_NAVIGATION_CLI to test a release CLI; otherwise use the sibling source build.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import paradise_assets
from paradise_assets import component_ops, navigation_ops, ui
from paradise_assets.document import component_schema, prefab, project, well_known
from paradise_assets.materialize import (
    load,
    navigation_geometry,
    navigation_preview,
    save,
    store,
    sync,
    workfile,
)
from paradise_assets.play import host

REPO = Path(__file__).resolve().parents[2]

NAV = "11111111-1111-4111-8111-111111111111"
ROOT = "aaaaaaaa-1111-4111-8111-111111111111"
RIGIDBODY = "b7ab4dd8-c8da-4dc2-9e5e-192fd74deb11"
GEOMETRY = "22222222-2222-4222-8222-222222222222"
SKINNED = "33333333-3333-4333-8333-333333333333"
MIXED_MESH = "44444444-4444-4444-8444-444444444444"
STATIC_MESH = "55555555-5555-4555-8555-555555555555"
SCHEMA = {"components": [{
    "id": NAV, "type": "Test.SceneNavigation", "fields": [{
        "name": "NavMeshFile", "type": "string", "authoredBy": "navmesh", "assetKinds": [".navmesh"],
    }],
}, {
    "id": GEOMETRY, "type": "Test.MobileProp", "fields": [{
        "name": "BakeGeometry", "type": "bool", "authoredBy": "navmesh-geometry", "default": False,
    }],
}, {
    "id": SKINNED, "type": "Test.SkinnedActor", "fields": [{
        "name": "Mesh", "type": "asset", "authoredBy": "mesh", "assetKinds": [".skinnedmesh"],
        "optional": True, "default": {},
    }],
}, {
    "id": MIXED_MESH, "type": "Test.MixedVisual", "fields": [{
        "name": "Mesh", "type": "asset", "authoredBy": "mesh", "assetKinds": [".mesh", ".skinnedmesh"],
    }],
}, {
    "id": STATIC_MESH, "type": "Test.StaticVisual", "fields": [{
        "name": "Mesh", "type": "asset", "authoredBy": "mesh", "assetKinds": [".mesh"],
    }],
}]}


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("PASS " + message)


class Layout:
    def __init__(self, records=None):
        self.records = records if records is not None else []

    def label(self, **options):
        self.records.append(("label", options))

    def operator(self, name, **options):
        self.records.append((name, options))
        return SimpleNamespace()

    def row(self, **_options):
        return Layout(self.records)

    def prop(self, *_args, **_options):
        raise AssertionError("The generated navmesh path must not draw an editable property")


def floor(scene, parent, name="Floor", collection=None):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(-10, -10, 0), (10, -10, 0), (10, 10, 0), (-10, 10, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    (scene.collection if collection is None else collection).objects.link(obj)
    obj.parent = parent
    return obj


def check_geometry(scene):
    owner = bpy.data.objects.new("Document owner", None)
    scene.collection.objects.link(owner)
    store.tag_object(owner, ROOT, [])
    surface = floor(scene, owner)
    surface.scale = (-1, 1, 1)
    surface.location = (2, 3, 4)
    surface.modifiers.new("Evaluated triangulation", "TRIANGULATE")

    collection = bpy.data.collections.new("Instance source")
    floor(scene, None, "Instance floor", collection)
    instance = bpy.data.objects.new("Collection instance", None)
    scene.collection.objects.link(instance)
    instance.parent = owner
    instance.instance_type = "COLLECTION"
    instance.instance_collection = collection
    instance.location = (40, 5, 6)

    moving = bpy.data.objects.new("Moving actor", None)
    scene.collection.objects.link(moving)
    store.tag_object(moving, "bbbbbbbb-1111-4111-8111-111111111111", [
        {"id": RIGIDBODY, "data": {"BodyType": "Dynamic"}},
    ])
    floor(scene, moving, "Moving floor")
    floor(scene, None, "Unowned mesh")

    payload = navigation_geometry.snapshot(scene)
    check(len(payload["vertices"]) == 8 and len(payload["indices"]) == 12,
          "evaluated collection instances participate; moving actors and unowned meshes do not")
    vertices = [Vector(vertex) for vertex in payload["vertices"]]
    check(any((vertex - Vector((-8, 4, -13))).length < 1e-5 for vertex in vertices),
          "world-space geometry converts Blender Z-up to engine Y-up")
    check(any((vertex - Vector((30, 6, 5))).length < 1e-5 for vertex in vertices),
          "collection-instance placement is baked into world-space vertices")
    for start in range(0, len(payload["indices"]), 3):
        a, b, c = [vertices[index] for index in payload["indices"][start:start + 3]]
        check((b - a).cross(c - a).y > 0, "mirrored and instanced floor triangles retain upward winding")
    for obj in list(scene.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(collection)


def make_project(root):
    (root / "assets/levels/nested").mkdir(parents=True)
    (root / ".editor").mkdir()
    (root / "assets/project.toml").write_text('schema_version = 1\nname = "navigation-test"\n')
    (root / ".editor/authoring-schema.json").write_text(json.dumps(SCHEMA))
    entry = prefab.PrefabObject.with_meta(ROOT, "Level")
    entry.components.extend([
        prefab.PrefabComponent(well_known.TRANSFORM_ID, "transform", {
            "Position": [0, 0, 0], "Rotation": [0, 0, 0, 1], "Scale": [1, 1, 1],
        }),
        prefab.PrefabComponent(NAV, "Test.SceneNavigation", {
            "NavMeshFile": "obsolete/navmesh.bin", "Unrecognized": {"Value": 1.234567890123},
        }),
    ])
    path = root / "assets/levels/nested/arena.prefab"
    path.write_text(prefab.dumps(prefab.PrefabDocument([entry])))
    return path, project.locate(str(path))


def read_component(path):
    return prefab.loads(path.read_text(), str(path)).root().component(NAV).data


def materialize(scene, path, layout):
    load.load_document(scene, prefab.loads(path.read_text(), str(path)), str(path), layout,
                       clear_startup=True)
    owner = store.object_with_guid(scene, ROOT)
    bpy.context.view_layer.objects.active = owner
    owner.select_set(True)
    floor(scene, owner)
    return owner


def check_geometry_opt_out(scene, owner):
    collection = bpy.data.collections.new("Optional instance source")
    floor(scene, None, "Optional instance mesh", collection)
    instance = bpy.data.objects.new("Optional instance", None)
    scene.collection.objects.link(instance)
    instance.parent = owner
    instance.instance_type = "COLLECTION"
    instance.instance_collection = collection
    instance.scale = (-1, 1, 1)
    store.tag_object(instance, "cccccccc-1111-4111-8111-111111111111", [{"id": GEOMETRY, "data": {}}])
    try:
        check(len(navigation_geometry.snapshot(scene)["vertices"]) == 4,
              "schema navigation geometry opt-out defaults exclude an instanced object subtree")
        store.tag_object(instance, "cccccccc-1111-4111-8111-111111111111", [
            {"id": GEOMETRY, "data": {"BakeGeometry": False}},
        ])
        check(len(navigation_geometry.snapshot(scene)["vertices"]) == 4,
              "an explicit navigation geometry opt-out excludes mirrored collection geometry")
        store.tag_object(instance, "cccccccc-1111-4111-8111-111111111111", [
            {"id": GEOMETRY, "data": {"BakeGeometry": True}},
        ])
        check(len(navigation_geometry.snapshot(scene)["vertices"]) == 8,
              "static mirrored collection geometry participates when opted in")
    finally:
        bpy.data.objects.remove(instance, do_unlink=True)
        bpy.data.collections.remove(collection)


def check_skinned_placeholder_exclusion(scene, owner):
    collection = bpy.data.collections.new("Actor placeholder source")
    placeholder = floor(scene, None, "Unrigged actor placeholder", collection)
    instance = bpy.data.objects.new("Actor placeholder instance", None)
    scene.collection.objects.link(instance)
    instance.parent = owner
    instance.instance_type = "COLLECTION"
    instance.instance_collection = collection
    instance.location.x = 40
    guid = "dddddddd-1111-4111-8111-111111111111"
    check(placeholder.type == "MESH" and not placeholder.modifiers and placeholder.animation_data is None,
          "the skinned-asset regression uses rigid placeholder geometry with no Blender rig or animation")
    try:
        for data in ({}, {"Mesh": None}, {"Mesh": {}}, {"Mesh": {"path": "models/actor.glb"}}):
            store.tag_object(instance, guid, [{"id": SKINNED, "data": data}])
            check(len(navigation_geometry.snapshot(scene)["vertices"]) == 4,
                  "strict skinned schema excludes rigid placeholders, including missing"
                  f" optional/default references ({data=})")
        for reference in ("models/actor.skinnedmesh", {"path": "models/actor.SKINNEDMESH"}):
            store.tag_object(instance, guid, [{"id": MIXED_MESH, "data": {"Mesh": reference}}])
            check(len(navigation_geometry.snapshot(scene)["vertices"]) == 4,
                  "a skinned reference excludes rigid placeholder geometry when the schema"
                  " also allows static meshes")
        for component_id in (STATIC_MESH, MIXED_MESH):
            store.tag_object(instance, guid, [{
                "id": component_id, "data": {"Mesh": {"path": "models/walkway.mesh"}},
            }])
            check(len(navigation_geometry.snapshot(scene)["vertices"]) == 8,
                  "a static .mesh reference retains unrigged collection geometry for navigation baking")
    finally:
        bpy.data.objects.remove(instance, do_unlink=True)
        bpy.data.collections.remove(collection)


def check_async_queue(scene, path, project_layout):
    original_bpy, original_start = navigation_ops.bpy, host.start_cli
    navigation_ops.bpy = SimpleNamespace(
        app=SimpleNamespace(background=False, timers=bpy.app.timers, handlers=bpy.app.handlers),
        data=bpy.data)
    output = path.with_suffix(".navmesh")
    previous = b"previous complete navmesh"
    output.write_bytes(previous)
    jobs = []

    class Job:
        def __init__(self, arguments):
            self.output = Path(arguments[arguments.index("--output") + 1])
            self.preview = Path(arguments[arguments.index("--preview") + 1])
            self.result = None
            self.closed = False

        def finish(self, data):
            self.output.write_bytes(data)
            self.preview.write_text(json.dumps({
                "vertices": [[0, 0, 0], [1, 0, 0], [0, 0, 1]], "indices": [0, 1, 2],
            }))
            self.result = host.CliResult(0, "", "")

        def poll(self):
            return self.result

        def close(self):
            self.closed = True

    def start(arguments, **_options):
        job = Job(arguments)
        jobs.append(job)
        return job

    host.start_cli = start
    try:
        navigation_ops.request(scene, bake=True)
        navigation_ops.request(scene, bake=True)
        abandoned = navigation_ops._QUEUED[scene.as_pointer()].directory
        navigation_ops.request(scene, bake=True)
        check(not abandoned.exists() and len(jobs) == 1,
              "rapid saves retain one pending bake and discard the superseded temporary snapshot")
        first_directory = jobs[0].output.parent
        jobs[0].finish(b"obsolete result")
        navigation_ops._poll()
        check(output.read_bytes() == previous and len(jobs) == 2 and not first_directory.exists(),
              "an obsolete in-flight bake cannot overwrite the previous binary;"
              " only the newest queued save starts")
        latest_directory = jobs[1].output.parent
        jobs[1].finish(b"latest result")
        check(navigation_ops._poll() is None and output.read_bytes() == b"latest result"
              and not latest_directory.exists(),
              "the latest queued bake is promoted and its temporary files are removed")
        navigation_ops.clear(scene)

        navigation_ops.request(scene, bake=True)
        for obj in list(scene.objects):
            if obj.type == "MESH":
                bpy.data.objects.remove(obj, do_unlink=True)
        bpy.ops.paradise_assets.toggle_navigation_auto_bake()
        failed = save.save_prefab(scene)
        check(failed.warnings and "No static document mesh geometry" in navigation_ops.error(scene),
              "a newer save reports missing bake geometry while an older bake is running")
        jobs[-1].finish(b"stale result after newer save")
        navigation_ops._poll()
        check(output.read_bytes() == b"latest result"
              and "No static document mesh geometry" in navigation_ops.error(scene),
              "an older bake cannot publish or clear the error when a newer save failed to prepare its bake")
        bpy.ops.paradise_assets.toggle_navigation_auto_bake()
        floor(scene, store.object_with_guid(scene, ROOT))
        navigation_ops.clear(scene)

        navigation_ops.request(scene, bake=True)
        navigation_ops.request(scene, bake=True)
        active_directory = jobs[-1].output.parent
        queued_directory = navigation_ops._QUEUED[scene.as_pointer()].directory
        materialize(scene, path, project_layout)
        check(jobs[-1].closed and not active_directory.exists() and not queued_directory.exists()
              and not navigation_ops.busy(scene),
              "reload terminates active baking and removes active and queued temporary data")
        check(not bpy.app.timers.is_registered(navigation_ops._poll),
              "reload removes the idle CLI polling timer")

        navigation_ops.request(scene, bake=True)
        active_directory = jobs[-1].output.parent
        navigation_ops.unregister_handler()
        check(jobs[-1].closed and not active_directory.exists() and not navigation_ops.busy(scene),
              "unregistering navigation terminates its CLI job and cleans temporary data")
        navigation_ops.register_handler()

        for queued in (False, True):
            deleted_scene = scene.copy()
            bpy.context.window.scene = deleted_scene
            navigation_ops.request(deleted_scene, bake=True)
            completed_job = jobs[-1]
            active_directory = completed_job.output.parent
            queued_directory = None
            if queued:
                navigation_ops.request(deleted_scene, bake=True)
                queued_directory = navigation_ops._QUEUED[deleted_scene.as_pointer()].directory
            bpy.context.window.scene = scene
            bpy.data.scenes.remove(deleted_scene)
            completed_job.finish(b"deleted scene result")
            count = len(jobs)
            check(navigation_ops._poll() is None and output.read_bytes() == b"latest result"
                  and not active_directory.exists() and len(jobs) == count,
                  "completing a deleted scene's bake discards its output"
                  f" without starting queued work ({queued=})")
            check(queued_directory is None or not queued_directory.exists(),
                  "deleted-scene completion also removes its pending snapshot")
            navigation_ops.clear(scene)

        deleted_scene = scene.copy()
        bpy.context.window.scene = deleted_scene
        navigation_ops.request(deleted_scene, bake=True)
        navigation_ops.request(deleted_scene, bake=True)
        active_directory = jobs[-1].output.parent
        queued_directory = navigation_ops._QUEUED[deleted_scene.as_pointer()].directory
        bpy.context.window.scene = scene
        bpy.data.scenes.remove(deleted_scene)
        navigation_ops.unregister_handler()
        check(jobs[-1].closed and not active_directory.exists() and not queued_directory.exists()
              and not navigation_ops._JOBS and not navigation_ops._QUEUED,
              "unregistering reaps active and queued jobs whose scene has already been deleted")
        check(not bpy.app.timers.is_registered(navigation_ops._poll),
              "unregistering after scene deletion removes the polling timer")
        navigation_ops.register_handler()
    finally:
        navigation_ops.clear(scene)
        navigation_ops.bpy, host.start_cli = original_bpy, original_start


def check_workflow(scene, root, cli):
    path, project_layout = make_project(root)
    owner = materialize(scene, path, project_layout)
    check_geometry_opt_out(scene, owner)
    check_skinned_placeholder_exclusion(scene, owner)
    schema = component_schema.load(str(root)).get(NAV)
    component = next(item for item in component_ops.components_of(owner) if item["id"] == NAV)
    drawn = Layout()
    before = dict(scene.items())
    ui._draw_schema_fields(drawn, bpy.context, owner, component, schema, {})
    check(dict(scene.items()) == before, "drawing the navigation controls does not modify scene properties")
    labels = [options for name, options in drawn.records if name == "label"]
    check(any(item.get("icon") == "LOCKED" and "levels/nested/arena.navmesh" in item["text"]
              for item in labels),
          "the read-only field displays the path derived from the level filename")
    controls = {name: options for name, options in drawn.records if name != "label"}
    check(set(controls) == {"paradise_assets.bake_navigation", "paradise_assets.toggle_navigation_preview",
                            "paradise_assets.toggle_navigation_auto_bake"},
          "the schema-driven component panel offers Bake, Preview, and Auto-bake on Save")
    for name in controls:
        category, action = name.split(".")
        getattr(getattr(bpy.ops, category), action).get_rna_type()
    check(not controls["paradise_assets.toggle_navigation_auto_bake"]["depress"]
          and not navigation_ops.auto_bake(scene), "automatic baking defaults to disabled")

    original_preference, original_run = host._preference, host.run_cli
    calls = []

    def run(arguments, **options):
        calls.append(list(arguments))
        if arguments[:2] == ["assets", "bake-navmesh"]:
            check(read_component(path)["NavMeshFile"] == "levels/nested/arena.navmesh",
                  "the canonical document is saved before the CLI starts baking")
        return original_run(arguments, **options)

    def bake_count():
        return sum(arguments[:2] == ["assets", "bake-navmesh"] for arguments in calls)

    host._preference = (lambda name, default="": str(cli) if name == "cli"
                        else original_preference(name, default))
    host.run_cli = run
    output = path.with_suffix(".navmesh")
    try:
        save.save_prefab(scene)
        check(bake_count() == 0 and not output.exists(),
              "ordinary Save does not bake while auto-bake is disabled")
        check(read_component(path)["Unrecognized"] == {"Value": 1.234567890123},
              "saving the generated path preserves unknown navigation payload fields")
        check(bpy.ops.paradise_assets.bake_navigation() == {"FINISHED"},
              "Bake succeeds using the real engine CLI")
        check(output.is_file() and output.stat().st_size > 100 and bake_count() == 1,
              "Bake creates a nonempty .navmesh beside the canonical level")
        check(not navigation_preview.is_visible(scene), "baking leaves a disabled preview disabled")

        objects_before = set(scene.objects.keys())
        check(bpy.ops.paradise_assets.toggle_navigation_preview() == {"FINISHED"}
              and navigation_preview.is_visible(scene),
              "Preview reads the baked Detour binary through the CLI")
        check(set(scene.objects.keys()) == objects_before,
              "enabling navigation preview creates no scene objects")
        bpy.ops.paradise_assets.toggle_navigation_preview()
        check(not navigation_preview.is_visible(scene), "Preview toggles back off")

        bpy.ops.paradise_assets.toggle_navigation_auto_bake()
        check(navigation_ops.auto_bake(scene), "Auto-bake on Save enables for the current document")
        count = bake_count()
        save.save_prefab(scene)
        check(bake_count() == count + 1, "Save performs exactly one automatic bake")
        count = bake_count()
        bpy.ops.paradise_assets.bake_navigation()
        check(bake_count() == count + 1,
              "the explicit Bake button does not recursively auto-bake its own save")
        count = bake_count()
        check(workfile.save(project_layout, str(path)) is not None,
              "the disposable working blend can be saved")
        check(bake_count() == count, "internal working-blend saves do not trigger another bake")
        bpy.ops.wm.save_mainfile()
        check(bake_count() == count + 1 and sync.refusal(scene) is None,
              "Ctrl+S performs exactly one automatic bake")

        previous_output = output.read_bytes()
        original_host_run = host.run_cli
        host.run_cli = lambda *_args, **_kwargs: host.CliResult(1, "", "error: deliberate bake failure")
        failed = save.save_prefab(scene)
        host.run_cli = original_host_run
        check(output.read_bytes() == previous_output, "a failed bake preserves the last usable binary")
        check(any("deliberate bake failure" in warning for warning in failed.warnings)
              and "deliberate bake failure" in navigation_ops.error(scene),
              "automatic bake failure is reported while the document save succeeds")

        count = bake_count()
        path.write_text(path.read_text() + "\n# external edit\n")
        try:
            save.save_prefab(scene)
        except save.SaveError:
            pass
        else:
            raise AssertionError("A stale document must refuse its save")
        check(bake_count() == count and output.read_bytes() == previous_output,
              "a refused canonical save never launches auto-bake")

        navigation_preview.show(scene, {"vertices": [[0, 0, 0], [1, 0, 0], [0, 0, 1]], "indices": [0, 1, 2]})
        materialize(scene, path, project_layout)
        check(not navigation_preview.is_visible(scene),
              "reloading the document clears the transient navigation preview")
        check(len(prefab.loads(path.read_text(), str(path)).objects) == 1,
              "preview and geometry helpers are never serialized as document objects")

        other = path.with_name("second.prefab")
        other.write_text(path.read_text())
        materialize(scene, other, project_layout)
        check(not navigation_ops.auto_bake(scene),
              "a newly opened document does not inherit another level's auto-bake toggle")
        save.save_prefab(scene)
        check(read_component(other)["NavMeshFile"] == "levels/nested/second.navmesh",
              "renaming a level derives a new navigation filename on save")
        check_async_queue(scene, other, project_layout)
        navigation_preview.show(scene, {"vertices": [[0, 0, 0], [1, 0, 0], [0, 0, 1]], "indices": [0, 1, 2]})
    finally:
        host._preference, host.run_cli = original_preference, original_run


def main():
    default_cli = REPO.parent / "ParadiseEngine/src/Paradise.Cli/bin/Debug/net10.0/paradise"
    cli = Path(os.environ.get("PARADISE_NAVIGATION_CLI", str(default_cli)))
    paradise_assets.register()
    try:
        scene = bpy.context.scene
        for obj in list(scene.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        check_geometry(scene)
        if not cli.is_file():
            if "PARADISE_NAVIGATION_CLI" in os.environ:
                raise AssertionError(f"Configured navigation CLI does not exist: {cli}")
            print("SKIP real navigation bake: build the sibling CLI or set PARADISE_NAVIGATION_CLI")
            return
        with tempfile.TemporaryDirectory(prefix="paradise-navigation-") as directory:
            check_workflow(scene, Path(directory), cli)
    finally:
        paradise_assets.unregister()
    check(not navigation_preview.is_visible(bpy.context.scene), "unregistering the addon clears the preview")
    check(navigation_ops._before_load not in bpy.app.handlers.load_pre
          and navigation_preview._loaded not in bpy.app.handlers.load_pre,
          "unregistering removes navigation lifecycle handlers")


if __name__ == "__main__":
    main()
