"""Editor-owned authored previews across native Blender saves and asynchronous results."""

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

from test_authored_actions import COMPONENT, ENTITY, check, document, make_project, owner
from test_panel import Layout

import paradise_assets
from paradise_assets import action_ops, edits
from paradise_assets.document import component_schema, prefab
from paradise_assets.materialize import action_preview, load, save, store
from paradise_assets.play import host

SCHEMA = {"components": [{"id": COMPONENT, "type": "Example.Shelf", "fields": [
    {"name": "Title", "type": "string"},
], "actions": [
    {"name": "Bake", "kind": "button", "onSave": True},
    {"name": "Rename", "kind": "button"},
    {"name": "AutoBake", "kind": "toggle"},
    {"name": "Surface", "kind": "preview"},
    {"name": "Edges", "kind": "preview"},
]}]}


class Server:
    def __init__(self):
        self.calls = []
        self.fail = set()
        self.revision = 0
        self.overlay_id = "shared"
        self.empty = False

    def __call__(self, arguments, *, cwd):
        command = list(arguments)
        path, action = Path(command[2]), command[4]
        state = json.loads(Path(command[command.index("--state") + 1]).read_text())
        value = command[command.index("--value") + 1] == "true" if "--value" in command else None
        self.calls.append((action, value, "--on-save" in command, state))
        if action in self.fail:
            return host.CliResult(1, "", "error: deliberate preview failure")
        result = {"toggles": {}, "documentChanged": False, "overlays": []}
        if action == "AutoBake":
            result["toggles"][action] = value
        elif action == "Bake":
            self.revision += 1
        elif action == "Rename":
            data = document(path)
            data.objects[0].component(COMPONENT).data["Title"] = "Generated title"
            path.write_text(prefab.dumps(data))
            result["documentChanged"] = True
        else:
            assert action in {"Surface", "Edges"}
            assert value is None and "--on-save" not in command
            assert "Surface" not in state and "Edges" not in state
            result["overlays"].append({"id": self.overlay_id, "visible": True,
                "vertices": [] if self.empty else [self.revision, 0, 0, 1, 0, 0, 0, 0, 1],
                "indices": [] if self.empty else [0, 1, 2]})
        Path(command[command.index("--response") + 1]).write_text(json.dumps(result))
        return host.CliResult(0, "", "")


def toggle(name, value):
    return bpy.ops.paradise_assets.toggle_preview(component_id=COMPONENT, entity_id=ENTITY,
                                                 action_name=name, value=value)


def visible(scene, path, provider, overlay="shared"):
    return action_preview.is_visible(scene, action_ops._preview_owner(str(path), ENTITY, COMPONENT, provider), overlay)


def names(server):
    return [call[0] for call in server.calls]


def check_workflow(scene, path, layout):
    server = Server()
    with patch.object(host, "run_cli", server):
        load.load_document(scene, document(path), str(path), layout)
        schema = component_schema.load(layout.root).get(COMPONENT)
        panel, before = Layout(), dict(scene.items())
        action_ops.draw(panel, bpy.context, owner(scene), COMPONENT, schema)
        check(dict(scene.items()) == before and panel.operators == ["paradise_assets.invoke_action"] * 3
              + ["paradise_assets.toggle_preview"] * 2,
              "preview controls are distinct from business toggles and draw without mutation")

        action_ops._set_toggles(scene, str(path), ENTITY, COMPONENT, {"Surface": True, "AutoBake": True})
        edits.set_field(owner(scene), COMPONENT, "Title", "Pending")
        count = len(scene.objects)
        check(toggle("Surface", True) == {"FINISHED"} and names(server) == ["Surface"],
              "enable requests geometry without invoking save hooks")
        check(document(path).objects[0].component(COMPONENT).data["Title"] == "Pending"
              and visible(scene, path, "Surface") and len(scene.objects) == count,
              "enable saves pending edits before drawing geometry without creating objects")
        check(server.calls[-1][3] == {"AutoBake": True}
              and action_ops.preview_enabled(scene, ENTITY, COMPONENT, "Surface")
              and "Surface" not in path.read_text(),
              "persisted preview visibility is separate from canonical data and business toggle state")

        server.calls.clear()
        bpy.ops.paradise_assets.invoke_action(component_id=COMPONENT, entity_id=ENTITY, action_name="Bake")
        check(names(server) == ["Bake", "Surface"] and server.revision == 1,
              "Bake refreshes enabled providers even when documentChanged is false")
        server.calls.clear()
        bpy.ops.paradise_assets.invoke_action(component_id=COMPONENT, entity_id=ENTITY, action_name="Rename")
        check(names(server) == ["Rename", "Surface"] and visible(scene, path, "Surface")
              and not store.read_state(scene).is_stale,
              "a canonical action refresh preserves visibility and requests fresh geometry exactly once")
        server.calls.clear()
        save.save_prefab(scene)
        check(names(server) == ["Bake", "Surface"] and server.calls[0][2],
              "save hooks finish before enabled previews refresh")

        toggle("Edges", True)
        check(visible(scene, path, "Surface") and visible(scene, path, "Edges"),
              "providers with identical overlay ids have independent ownership")
        server.calls.clear()
        toggle("Surface", False)
        check(not server.calls and not visible(scene, path, "Surface") and visible(scene, path, "Edges"),
              "disable is local and leaves the other provider's matching overlay intact")

        server.overlay_id = "replacement"
        save.save_prefab(scene)
        check(not visible(scene, path, "Edges") and visible(scene, path, "Edges", "replacement"),
              "a provider refresh replaces its complete previous overlay set")
        server.empty = True
        save.save_prefab(scene)
        check(not visible(scene, path, "Edges", "replacement"), "empty geometry clears the previous preview")
        server.empty, server.overlay_id = False, "shared"
        save.save_prefab(scene)

        server.calls.clear()
        load.load_document(scene, document(path), str(path), layout)
        check(names(server) == ["AutoBake", "Edges"] and visible(scene, path, "Edges")
              and not action_ops.preview_enabled(scene, ENTITY, COMPONENT, "Surface")
              and not any(call[2] for call in server.calls),
              "reload restores enabled preview visibility separately from business toggles without save hooks")

        other = path.with_name("other.prefab")
        other.write_text(path.read_text())
        load.load_document(scene, document(other), str(other), layout)
        check(not action_ops.preview_enabled(scene, ENTITY, COMPONENT, "Edges"),
              "another document does not inherit the provider visibility")
        load.load_document(scene, document(path), str(path), layout)
        check(visible(scene, path, "Edges"), "returning to a document restores its saved preview visibility")

        server.fail.add("Edges")
        save.save_prefab(scene)
        check(not visible(scene, path, "Edges") and "deliberate preview failure" in action_ops.error(scene)
              and action_ops.preview_enabled(scene, ENTITY, COMPONENT, "Edges"),
              "failed refresh clears stale geometry but preserves the author's visibility preference")
        server.fail.clear()
        save.save_prefab(scene)
        check(visible(scene, path, "Edges"), "a later save recovers the enabled preview after a failure")
        toggle("Edges", False)
        action_ops._set_toggles(scene, str(path), ENTITY, COMPONENT, {"AutoBake": False})


def check_async(scene, path, layout):
    server, jobs = Server(), []

    class Job:
        def __init__(self, arguments, cwd):
            self.arguments, self.cwd, self.result, self.closed = arguments, cwd, None, False

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

    def complete():
        jobs[-1].complete()
        action_ops._poll()

    runtime = SimpleNamespace(app=SimpleNamespace(background=False, timers=bpy.app.timers,
                                                handlers=bpy.app.handlers), context=bpy.context, data=bpy.data)
    with patch.object(action_ops, "bpy", runtime), patch.object(host, "start_cli", start):
        toggle("Surface", True)
        action_ops._refresh_previews(scene)
        action_ops._refresh_previews(scene)
        check(len(action_ops._QUEUED[scene.as_pointer()]) == 1,
              "repeated refreshes deduplicate queued requests for the same provider")
        count = len(jobs)
        toggle("Surface", False)
        complete()
        check(len(jobs) == count and not visible(scene, path, "Surface") and not action_ops.busy(scene),
              "disable during a pending preview removes queued work and ignores the late result")

        toggle("Surface", True)
        edits.set_field(owner(scene), COMPONENT, "Title", "Newer edit")
        complete()
        check(not visible(scene, path, "Surface") and "local edits changed" in action_ops.error(scene)
              and edits.read(owner(scene))[COMPONENT]["Title"] == "Newer edit",
              "preview results computed before local edits cannot display stale geometry or overwrite edits")
        save.save_prefab(scene, invoke_actions=False)
        action_ops._refresh_previews(scene)
        complete()
        check(visible(scene, path, "Surface"), "a fresh request draws the saved edit after stale-result rejection")

        server.calls.clear()
        action_ops.request(scene, ENTITY, COMPONENT, "Bake")
        action_ops.request(scene, ENTITY, COMPONENT, "Bake")
        action_ops._refresh_previews(scene)
        complete()
        complete()
        complete()
        check(names(server) == ["Bake", "Bake", "Surface"] and not action_ops.busy(scene),
              "queued business actions finish before a single provider refresh without recursion")

        server.calls.clear()
        action_ops.request(scene, ENTITY, COMPONENT, "Bake")
        action_ops._refresh_previews(scene)
        edits.set_field(owner(scene), COMPONENT, "Title", "Edited during bake")
        complete()
        check(names(server) == ["Bake"] and not action_ops.busy(scene) and not visible(scene, path, "Surface")
              and "save to refresh" in action_ops.error(scene),
              "edits during Bake cannot let a queued preview treat unsaved geometry as canonical")
        save.save_prefab(scene, invoke_actions=False)
        action_ops._refresh_previews(scene)
        complete()

        action_ops._refresh_previews(scene)
        path.write_text(path.read_text() + "\n# external edit\n")
        complete()
        check(not visible(scene, path, "Surface") and store.read_state(scene).is_stale,
              "an external document change invalidates a pending geometry response")
        load.load_document(scene, document(path), str(path), layout)
        complete()

        action_ops._refresh_previews(scene)
        schema_path = Path(layout.root, ".editor/authoring-schema.json")
        removed = json.loads(json.dumps(SCHEMA))
        removed["components"][0]["actions"] = [item for item in removed["components"][0]["actions"]
                                                   if item["name"] != "Surface"]
        schema_path.write_text(json.dumps(removed))
        complete()
        check(not visible(scene, path, "Surface")
              and not action_ops.preview_enabled(scene, ENTITY, COMPONENT, "Surface"),
              "removing a schema provider prunes its visibility and rejects its pending response")
        schema_path.write_text(json.dumps(SCHEMA))

        toggle("Surface", True)
        complete()
        schema_path.write_text(json.dumps(removed))
        action_ops._prune_pending()
        check(not visible(scene, path, "Surface")
              and not action_ops.preview_enabled(scene, ENTITY, COMPONENT, "Surface"),
              "enabled-preview polling detects schema removal without a depsgraph event")
        schema_path.write_text(json.dumps(SCHEMA))

        toggle("Surface", True)
        edits.remove_component(owner(scene), COMPONENT)
        action_ops.prune(scene)
        complete()
        check(not visible(scene, path, "Surface")
              and not action_ops.preview_enabled(scene, ENTITY, COMPONENT, "Surface"),
              "component removal invalidates the pending provider and its saved visibility")
        load.load_document(scene, document(path), str(path), layout)

        toggle("Surface", True)
        bpy.data.objects.remove(owner(scene), do_unlink=True)
        action_ops._prune_pending()
        complete()
        check(not visible(scene, path, "Surface")
              and not action_ops.preview_enabled(scene, ENTITY, COMPONENT, "Surface"),
              "entity deletion invalidates the pending provider and its saved visibility")
        load.load_document(scene, document(path), str(path), layout)

        toggle("Surface", True)
        pending = jobs[-1]
        directory = action_ops._JOBS[scene.as_pointer()].directory
        load.load_document(scene, document(path), str(path), layout)
        check(pending.closed and not directory.exists() and action_ops.busy(scene),
              "reload cancels old preview work and restores visibility with a new request")
        complete()
        check(visible(scene, path, "Surface"), "reload's new provider request restores the preview")
        toggle("Surface", False)

        removed_scene = bpy.data.scenes.new("Pending preview")
        with bpy.context.temp_override(scene=removed_scene, view_layer=removed_scene.view_layers[0]):
            load.load_document(removed_scene, document(path), str(path), layout)
            toggle("Surface", True)
        key = removed_scene.as_pointer()
        directory = action_ops._JOBS[key].directory
        jobs[-1].complete()
        bpy.data.scenes.remove(removed_scene)
        action_ops._poll()
        check(key not in action_ops._JOBS and not directory.exists(),
              "scene deletion during a pending preview cleans up without aborting the polling timer")


def main():
    paradise_assets.register()
    try:
        with tempfile.TemporaryDirectory(prefix="authored-previews-") as directory:
            root = Path(directory)
            path, layout = make_project(root)
            (root / ".editor/authoring-schema.json").write_text(json.dumps(SCHEMA))
            scene = bpy.context.scene
            check_workflow(scene, path, layout)
            check_async(scene, path, layout)
    finally:
        paradise_assets.unregister()
    check(not action_preview._OVERLAYS and not action_ops._JOBS and not action_ops._QUEUED,
          "unregister removes all preview jobs and overlays")


if __name__ == "__main__":
    main()
