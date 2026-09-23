"""Generic authored actions across real Blender save, UI and asynchronous job lifecycles."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_panel import Layout

import paradise_assets
from paradise_assets import action_ops, edits
from paradise_assets.document import actions, component_schema, prefab, project
from paradise_assets.materialize import action_preview, load, save, store, sync
from paradise_assets.play import host

COMPONENT = "11111111-1111-4111-8111-111111111111"
ENTITY = "aaaaaaaa-1111-4111-8111-111111111111"
SCHEMA = {"components": [{"id": COMPONENT, "type": "Example.Shelf", "fields": [
    {"name": "Title", "type": "string"},
], "actions": [
    {"name": "Reindex", "displayName": "Refresh Search", "kind": "button"},
    {"name": "Reindex", "displayName": "Refresh Search", "kind": "save"},
    {"name": "Highlight", "displayName": "Show Bounds", "kind": "toggle"},
    {"name": "BackgroundIndex", "displayName": "Index on Save", "kind": "toggle"},
]}]}


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("PASS " + message)


def document(path):
    return prefab.loads(path.read_text(), str(path))


def owner(scene):
    return store.object_with_guid(scene, ENTITY)


def make_project(root):
    (root / "assets/levels").mkdir(parents=True)
    (root / ".editor").mkdir()
    (root / "assets/project.toml").write_text('schema_version = 1\nname = "authored-actions"\n')
    (root / ".editor/authoring-schema.json").write_text(json.dumps(SCHEMA))
    entry = prefab.PrefabObject.with_meta(ENTITY, "Shelf")
    entry.components.append(prefab.PrefabComponent(COMPONENT, "Example.Shelf", {
        "Title": "Original", "Future": {"Precision": 0.12345678901234567, "Flags": [1, 2, 3]},
    }))
    path = root / "assets/levels/shelf.prefab"
    path.write_text(prefab.dumps(prefab.PrefabDocument([entry])))
    return path, project.locate(str(path))


class Server:
    def __init__(self):
        self.calls = []
        self.fail = False

    def __call__(self, arguments, *, cwd):
        command = list(arguments)
        check(command[:2] == ["assets", "invoke-action"], "proxy invokes the generic CLI command")
        path, component, action = Path(command[2]), command[3], command[4]
        state = json.loads(Path(command[command.index("--state") + 1]).read_text())
        value = command[command.index("--value") + 1] == "true" if "--value" in command else None
        self.calls.append((action, value, "--on-save" in command, state))
        if self.fail:
            return host.CliResult(1, "", "error: deliberate action failure")
        result = {"toggles": {}, "documentChanged": False, "overlays": []}
        if value is not None:
            result["toggles"][action] = value
        if action == "Highlight":
            result["overlays"].append({"id": "bounds", "visible": value,
                                       "vertices": [0, 0, 0, 1, 0, 0, 0, 0, 1],
                                       "indices": [0, 1, 2], "color": [0, 1, 0, 0.4]})
        if action == "Reindex" and ("--on-save" not in command or state.get("BackgroundIndex")):
            data = document(path)
            data.objects[0].component(component).data["Generated"] = "C# owns this value"
            path.write_text(prefab.dumps(data))
            result["documentChanged"] = True
        Path(command[command.index("--response") + 1]).write_text(json.dumps(result))
        return host.CliResult(0, "", "")


def invoke(name, value=False):
    return bpy.ops.paradise_assets.invoke_action(component_id=COMPONENT, entity_id=ENTITY,
                                                action_name=name, value=value)


def check_workflow(scene, path, layout):
    server = Server()
    with patch.object(host, "run_cli", server):
        load.load_document(scene, document(path), str(path), layout)
        schema = component_schema.load(layout.root).get(COMPONENT)
        before = dict(scene.items())
        panel = Layout()
        action_ops.draw(panel, bpy.context, owner(scene), COMPONENT, schema)
        check(dict(scene.items()) == before, "drawing action controls does not mutate scene state")
        check(panel.operators == ["paradise_assets.invoke_action"] * 3,
              "unrelated custom component gets generic button and toggle operators")
        check([item.action_name for _, item in panel.run["props"]] ==
              ["Reindex", "Highlight", "BackgroundIndex"], "action names come from the schema")

        edits.set_field(owner(scene), COMPONENT, "Title", "Pending")
        check(invoke("Reindex") == {"FINISHED"}, "button action completes")
        payload = document(path).objects[0].component(COMPONENT).data
        check(payload["Title"] == "Pending" and payload["Future"]["Flags"] == [1, 2, 3],
              "manual action saves pending edits and preserves unknown payloads")
        check(len(server.calls) == 1 and not server.calls[0][2],
              "manual action does not recursively dispatch on-save actions")
        check(not store.read_state(scene).is_stale and "Generated" in str(store.component_json(owner(scene))),
              "C# canonical changes refresh display snapshots and document stamp")

        count = len(scene.objects)
        invoke("Highlight", True)
        overlay_owner = action_ops._owner(str(path), ENTITY, COMPONENT)
        check(action_ops.toggle_values(scene, ENTITY, COMPONENT)["Highlight"]
              and action_preview.is_visible(scene, overlay_owner, "bounds"),
              "toggle applies returned state and generic overlay")
        check(len(scene.objects) == count, "overlays create no document or helper objects")
        invoke("Highlight", False)
        check(not action_preview.is_visible(scene, overlay_owner, "bounds"), "toggle hides overlay")

        invoke("BackgroundIndex", True)
        invoke("Highlight", True)
        before_reload = len(server.calls)
        load.load_document(scene, document(path), str(path), layout)
        check([call[0] for call in server.calls[before_reload:]] == ["Highlight", "BackgroundIndex"]
              and not any(call[2] for call in server.calls[before_reload:])
              and action_preview.is_visible(scene, overlay_owner, "bounds"),
              "reload replays enabled toggle callbacks and restores effects without save hooks")
        save.save_prefab(scene)
        check(server.calls[-1][0] == "Reindex" and server.calls[-1][2]
              and server.calls[-1][3]["BackgroundIndex"], "save dispatches declared hook and current toggle state")
        sync._sync(scene)
        check(server.calls[-1][2] and sync.refusal(scene) is None, "Ctrl+S follows the same generic save hook")
        check("BackgroundIndex" not in path.read_text(), "toggle state never enters canonical component data")

        server.fail = True
        result = save.save_prefab(scene)
        check(result.warnings and "deliberate action failure" in action_ops.error(scene),
              "action failure reports separately from a successful document save")
        server.fail = False

        invoke("Highlight", False)

        other = path.with_name("other.prefab")
        other.write_text(path.read_text())
        load.load_document(scene, document(other), str(other), layout)
        check(action_ops.toggle_values(scene, ENTITY, COMPONENT) == {},
              "another document never inherits action toggle state")
        load.load_document(scene, document(path), str(path), layout)
        server.fail = True
        load.load_document(scene, document(path), str(path), layout)
        check(not action_ops.toggle_values(scene, ENTITY, COMPONENT)["BackgroundIndex"],
              "failed toggle restoration does not claim an enabled effect")
        server.fail = False


def check_async(scene, path, layout):
    server = Server()
    jobs = []

    class Job:
        def __init__(self, arguments, cwd):
            self.arguments, self.cwd = arguments, cwd
            self.result = None
            self.closed = False

        def poll(self):
            return self.result

        def complete(self):
            self.result = server(self.arguments, cwd=self.cwd)

        def close(self):
            self.closed = True

    def start(arguments, *, cwd):
        job = Job(arguments, cwd)
        jobs.append(job)
        return job

    runtime = SimpleNamespace(app=SimpleNamespace(background=False, timers=bpy.app.timers,
                                                handlers=bpy.app.handlers),
                              context=bpy.context, data=bpy.data)
    with patch.object(action_ops, "bpy", runtime), patch.object(host, "start_cli", start):
        action_ops.request(scene, ENTITY, COMPONENT, "Highlight", value=True)
        action_ops.request(scene, ENTITY, COMPONENT, "BackgroundIndex", value=False)
        check(action_ops.busy(scene) and len(jobs) == 1, "async actions queue without overlapping writes")
        try:
            save.save_prefab(scene)
        except save.SaveError:
            pass
        else:
            raise AssertionError("A pending action must guard document writes")
        jobs[0].complete()
        action_ops._poll()
        check(len(jobs) == 2, "queued action starts after the first response")
        jobs[1].complete()
        action_ops._poll()
        check(not action_ops.busy(scene), "completed async jobs release the document")

        selected_helper = bpy.data.objects.new("Selected helper", None)
        untouched = bpy.data.objects.new("Unselected extra", None)
        scene.collection.objects.link(selected_helper)
        scene.collection.objects.link(untouched)
        layer = scene.view_layers[0]
        layer.update()
        selected_helper.select_set(True, view_layer=layer)
        untouched.select_set(False, view_layer=layer)
        layer.objects.active = selected_helper
        other = bpy.data.scenes.new("Other scene")
        other_object = bpy.data.objects.new("Other active", None)
        other.collection.objects.link(other_object)
        other.view_layers[0].objects.active = other_object
        try:
            action_ops.request(scene, ENTITY, COMPONENT, "Reindex")
            bpy.context.window.scene = other
            jobs[-1].complete()
            action_ops._poll()
            check(not store.read_state(scene).is_stale and bpy.context.scene == other
                  and other.view_layers[0].objects.active == other_object,
                  "action refresh updates its inactive scene without changing the active scene")
            check(selected_helper.select_get(view_layer=layer) and not untouched.select_get(view_layer=layer),
                  "a selected guid-less helper never selects unrelated guid-less objects")
        finally:
            bpy.context.window.scene = scene
            bpy.data.scenes.remove(other)

        action_ops.request(scene, ENTITY, COMPONENT, "Reindex")
        pending = action_ops._JOBS[scene.as_pointer()]
        directory = pending.directory
        edits.set_field(owner(scene), COMPONENT, "Title", "While running")
        owner(scene).location.x += 2
        jobs[-1].complete()
        action_ops._poll()
        check(edits.read(owner(scene))[COMPONENT]["Title"] == "While running"
              and owner(scene).location.x == 2, "completed action never overwrites newer fields or transforms")
        check(store.read_state(scene).is_stale and "preserved" in action_ops.error(scene),
              "conflicting view refresh reports reconciliation instead of clearing new edits")
        check(not directory.exists(), "completed actions clean temporary transport files")
        action_ops._set_toggles(scene, str(path), ENTITY, COMPONENT, {"Highlight": False})
        load.load_document(scene, document(path), str(path), layout)

        action_ops.request(scene, ENTITY, COMPONENT, "Highlight", value=True)
        pending = action_ops._JOBS[scene.as_pointer()]
        directory = pending.directory
        load.load_document(scene, document(path), str(path), layout)
        check(jobs[-1].closed and not directory.exists() and not action_ops.busy(scene),
              "document reload cancels CLI jobs and clears temporary data")
        action_ops.request(scene, ENTITY, COMPONENT, "Highlight", value=True)
        action_ops.unregister_handler()
        check(jobs[-1].closed and not action_ops._JOBS and not action_ops._QUEUED,
              "addon cleanup cancels all remaining jobs")
        action_ops.register_handler()


def check_removed_owners(scene, path, layout):
    overlay_owner = action_ops._owner(str(path), ENTITY, COMPONENT)
    overlays = actions.response({"overlays": [{"id": "bounds", "visible": True,
        "vertices": [0, 0, 0, 1, 0, 0, 0, 0, 1], "indices": [0, 1, 2]}]}).overlays
    action_preview.apply(scene, overlay_owner, overlays)
    action_ops._set_toggles(scene, str(path), ENTITY, COMPONENT, {"Highlight": True})
    edits.remove_component(owner(scene), COMPONENT)
    action_ops.prune(scene)
    check(not action_preview.is_visible(scene, overlay_owner, "bounds")
          and not action_ops.toggle_values(scene, ENTITY, COMPONENT),
          "removing a component prunes only its generic overlays and toggle state")
    edits.clear(owner(scene))
    action_preview.apply(scene, overlay_owner, overlays)
    action_ops._set_toggles(scene, str(path), ENTITY, COMPONENT, {"Highlight": True})
    bpy.data.objects.remove(owner(scene), do_unlink=True)
    action_ops._prune_pending()
    check(not action_preview.is_visible(scene, overlay_owner, "bounds")
          and not action_ops.toggle_values(scene, ENTITY, COMPONENT),
          "deleted entity overlays are pruned by the deferred scene update")
    load.load_document(scene, document(path), str(path), layout)


def main():
    paradise_assets.register()
    try:
        with tempfile.TemporaryDirectory(prefix="authored-actions-") as directory:
            path, layout = make_project(Path(directory))
            scene = bpy.context.scene
            check_workflow(scene, path, layout)
            check_async(scene, path, layout)
            check_removed_owners(scene, path, layout)
    finally:
        paradise_assets.unregister()
    check(not action_preview._OVERLAYS and action_ops._before_load not in bpy.app.handlers.load_pre,
          "unregister removes action overlays and handlers")


if __name__ == "__main__":
    main()
