"""Reference-aware selection preflight; no CLI, watcher, model export or addon registration.

Run with Blender --background --factory-startup --python this_file.py.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bpy
from mathutils import Matrix, Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paradise_assets import edits
from paradise_assets.document import new_prefab, prefab, resolve, well_known
from paradise_assets.document.asset_reference import AssetReference
from paradise_assets.document.canonical_toml import InlineTable
from paradise_assets.document.prefab import PrefabComponent, PrefabObject
from paradise_assets.document.project import ProjectLayout
from paradise_assets.materialize import meshes, selection_prefab, store


def identity():
    return str(uuid.uuid4())


class SelectionPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.layout = ProjectLayout(self.temporary.name)
        Path(self.layout.assets).mkdir()
        Path(self.layout.editor).mkdir()
        self.mesh_id = identity()
        Path(self.layout.editor, "authoring-schema.json").write_text(json.dumps({
            "components": [{"id": self.mesh_id, "type": "Game.Mesh", "fields": [
                {"name": "Geometry", "authoredBy": "mesh"}]}]
        }), encoding="utf-8")
        self.scene = bpy.data.scenes.new("Selection preparation test")
        self.addCleanup(self.clear_scene)
        self.mesh_ref = self.asset("box.mesh", "schema_version = 1\n")
        self.material_ref = self.asset("box.material", "schema_version = 1\n")

    def clear_scene(self):
        for obj in list(self.scene.objects):
            data = obj.data if obj.type == "MESH" else None
            bpy.data.objects.remove(obj, do_unlink=True)
            if data is not None and data.users == 0:
                bpy.data.meshes.remove(data)
        bpy.data.scenes.remove(self.scene)

    def asset(self, path, text):
        destination = Path(self.layout.resolve(path))
        destination.write_text(text, encoding="utf-8")
        asset_id = identity()
        Path(str(destination) + ".meta").write_text(
            f'schema_version = 1\nguid = "{asset_id}"\n', encoding="utf-8")
        return AssetReference(asset_id, path)

    def mesh_entry(self, name="Mesh", parent=None):
        entry = PrefabObject.with_meta(identity(), name, parent)
        entry.components.append(PrefabComponent(self.mesh_id, "Game.Mesh", {
            "Geometry": InlineTable(guid=self.mesh_ref.guid, path=self.mesh_ref.path),
            "Material": InlineTable(guid=self.material_ref.guid, path=self.material_ref.path),
            "Unknown": {"Value": 17},
        }))
        return entry

    def obj(self, name="Object", entry=None, mesh=False, position=(0, 0, 0)):
        data = bpy.data.meshes.new(name) if mesh else None
        obj = bpy.data.objects.new(name, data)
        self.scene.collection.objects.link(obj)
        obj.matrix_world = Matrix.Translation(position)
        if entry is not None:
            # Deliberately poisonous display data: prepare must read the file instead.
            store.tag_object(obj, entry.guid, [{"id": self.mesh_id, "type": "Game.Mesh",
                                               "data": {"Geometry": "wrong.mesh"}}])
            store.tag_name(obj, entry.name)
        return obj

    def current(self, document):
        path = Path(self.layout.resolve("level.prefab"))
        path.write_text(prefab.dumps(document), encoding="utf-8")
        store.write_state(self.scene, str(path))
        return path

    def prepare(self, objects, active=None, mode="OBJECT"):
        context = SimpleNamespace(selected_objects=objects, active_object=active or objects[0],
                                  mode=mode, scene=self.scene)
        return selection_prefab.prepare(context, self.layout, "Selection")

    def prefab_instance(self, with_child=False):
        target = new_prefab.root_only("Existing")
        child = None
        if with_child:
            child = self.mesh_entry("Part", target.root_guid)
            target.objects.append(child)
        reference = self.asset("existing.prefab", prefab.dumps(target))
        obj = self.obj("Instance", position=(3, 4, 5))
        store.tag_object(obj, identity(), [])
        store.tag_prefab(obj, reference.guid, reference.path)
        store.tag_children(obj, [])
        return obj, reference, target, child

    def test_reference_only_new_instance_keeps_asset_and_live_transform(self):
        obj, reference, _, _ = self.prefab_instance()
        obj.matrix_world = Matrix.Translation((3, 4, 5)) @ Matrix.Rotation(0.4, 4, "Z")
        before = obj.matrix_world.copy()
        markers = dict(obj.items())
        with patch("paradise_assets.materialize.geometry.export", side_effect=AssertionError("export")):
            plan = self.prepare([obj])
            result = selection_prefab.compose(plan, None)
        self.assertEqual(plan.raw, [])
        self.assertTrue(plan.has_references)
        self.assertEqual(plan.origin, Vector((3, 4, 5)))
        self.assertEqual(result.root().component(well_known.TRANSFORM_ID).data,
                         new_prefab.IDENTITY_TRANSFORM)
        copied = result.objects[1]
        self.assertEqual(copied.prefab, reference)
        self.assertNotEqual(copied.guid, store.guid_of(obj))
        self.assertEqual(copied.parent, result.root_guid)
        transform = copied.component(well_known.TRANSFORM_ID).data
        self.assertEqual(transform[well_known.POSITION], [0.0, 0.0, 0.0])
        self.assertNotEqual(transform[well_known.ROTATION], [0.0, 0.0, 0.0, 1.0])
        self.assertEqual(obj.matrix_world, before)
        self.assertEqual(dict(obj.items()), markers)

    def test_mixed_selection_origin_comes_from_active_reference(self):
        obj, _, _, _ = self.prefab_instance()
        raw = self.obj("Raw", mesh=True, position=(8, 9, 10))
        plan = self.prepare([raw, obj], obj)
        self.assertEqual(plan.raw, [raw])
        self.assertEqual(plan.origin, Vector((3, 4, 5)))
        self.assertEqual(len(plan.document.objects), 2)

    def test_mesh_document_components_authoritative_and_deep_copied(self):
        document = new_prefab.root_only("Level")
        entry = self.mesh_entry(parent=document.root_guid)
        document.objects.append(entry)
        path = self.current(document)
        before = path.read_bytes()
        obj = self.obj(entry=entry, position=(4, 5, 6))
        plan = self.prepare([obj])
        copied = plan.document.objects[1]
        self.assertEqual(copied.component(self.mesh_id).data, entry.component(self.mesh_id).data)
        self.assertEqual(plan.raw, [])
        self.assertNotEqual(copied.guid, entry.guid)
        copied.component(self.mesh_id).data["Unknown"]["Value"] = 99
        self.assertEqual(entry.component(self.mesh_id).data["Unknown"]["Value"], 17)
        self.assertEqual(path.read_bytes(), before)

    def test_authored_prefab_reference_works_without_cached_marker(self):
        obj, reference, _, _ = self.prefab_instance()
        document = new_prefab.root_only("Level")
        entry = PrefabObject.with_meta(store.guid_of(obj), "Instance", document.root_guid)
        entry.prefab = reference
        document.objects.append(entry)
        self.current(document)
        del obj[store.PREFAB_KEY]
        self.assertEqual(self.prepare([obj]).document.objects[1].prefab, reference)

    def test_selected_mesh_hierarchy_preserves_world_placement(self):
        document = new_prefab.root_only("Level")
        parent_entry = self.mesh_entry("Parent", document.root_guid)
        child_entry = self.mesh_entry("Child", parent_entry.guid)
        document.objects.extend([parent_entry, child_entry])
        self.current(document)
        parent = self.obj("Parent", parent_entry, position=(2, 3, 4))
        child = self.obj("Child", child_entry)
        child.parent = parent
        child.matrix_world = Matrix.Translation((8, 10, 12))
        plan = self.prepare([child, parent], parent)
        copied = {entry.name: entry for entry in plan.document.objects}
        self.assertEqual(copied["Child"].parent, copied["Parent"].guid)
        self.assertEqual(copied["Child"].component(well_known.TRANSFORM_ID).data[well_known.POSITION],
                         [6.0, 8.0, -7.0])

    def test_owner_and_derived_child_do_not_duplicate_geometry(self):
        owner, reference, target, child_entry = self.prefab_instance(with_child=True)
        child_id = resolve.mint_child_guid(store.guid_of(owner), child_entry.guid)
        child = self.obj("Part", child_entry, mesh=True)
        store.tag_object(child, child_id, [])
        store.mark_derived(child)
        store.tag_local(child, store.guid_of(owner), child_entry.guid, True)
        store.tag_children(owner, [child_entry.guid])
        child.parent = owner
        child.matrix_world = owner.matrix_world @ Matrix.Translation((0, 2, 0))
        plan = self.prepare([owner, child], owner)
        self.assertEqual(plan.raw, [])
        instances = [entry for entry in plan.document.objects if entry.prefab]
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].prefab, reference)
        carriers = [entry for entry in plan.document.objects if entry.target]
        self.assertEqual(len(carriers), 1)
        self.assertEqual(carriers[0].target, child_entry.guid)
        self.assertEqual(carriers[0].parent, instances[0].guid)
        resolved = resolve.resolve(plan.document, lambda ref: target)
        self.assertEqual(resolved.errors, [])
        self.assertEqual(len(resolved.document.objects), 3)
        with self.assertRaisesRegex(new_prefab.CreateError, "owning instance"):
            self.prepare([child])

    def test_stale_document_and_missing_tagged_entry_refuse_export(self):
        document = new_prefab.root_only("Level")
        entry = self.mesh_entry(parent=document.root_guid)
        document.objects.append(entry)
        path = self.current(document)
        obj = self.obj(entry=entry, mesh=True)
        path.write_text(path.read_text() + "\n# changed\n", encoding="utf-8")
        with self.assertRaisesRegex(new_prefab.CreateError, "changed on disk"):
            self.prepare([obj])
        store.write_state(self.scene, str(path))
        store.tag_object(obj, identity(), [])
        with self.assertRaisesRegex(new_prefab.CreateError, "not in the current document"):
            self.prepare([obj])

    def test_broken_mismatched_and_cyclic_prefab_refs_refused(self):
        obj, reference, target, _ = self.prefab_instance()
        obj[store.PREFAB_KEY] = "[]"
        with self.assertRaisesRegex(new_prefab.CreateError, "broken prefab marker"):
            self.prepare([obj])
        store.tag_prefab(obj, identity(), reference.path)
        with self.assertRaisesRegex(new_prefab.CreateError, "mismatched identity"):
            self.prepare([obj])
        store.tag_prefab(obj, reference.guid, reference.path)
        target.root().prefab = reference
        Path(self.layout.resolve(reference.path)).write_text(prefab.dumps(target), encoding="utf-8")
        with self.assertRaisesRegex(new_prefab.CreateError, "cycle"):
            self.prepare([obj])
        Path(self.layout.resolve(reference.path)).unlink()
        with self.assertRaisesRegex(new_prefab.CreateError, "missing"):
            self.prepare([obj])

    def test_unsaved_components_shear_and_missing_mesh_refused(self):
        document = new_prefab.root_only("Level")
        entry = self.mesh_entry(parent=document.root_guid)
        document.objects.append(entry)
        self.current(document)
        obj = self.obj(entry=entry)
        edits.set_field(obj, self.mesh_id, "Unknown/Value", 55)
        with self.assertRaisesRegex(new_prefab.CreateError, "unsaved component edits"):
            self.prepare([obj])
        edits.clear(obj)
        parent = self.obj("Scaled parent")
        parent.scale = (2, 1, 1)
        obj.parent = parent
        obj.rotation_euler.z = 0.4
        self.scene.view_layers[0].update()
        with self.assertRaisesRegex(new_prefab.CreateError, "shear"):
            self.prepare([obj])
        obj.parent = None
        obj.matrix_world = Matrix.Identity(4)
        Path(self.layout.resolve(self.mesh_ref.path)).unlink()
        with self.assertRaisesRegex(new_prefab.CreateError, "missing"):
            self.prepare([obj])

    def test_raw_restrictions_and_lightweight_ui_helper(self):
        raw = self.obj("Raw", mesh=True)
        group = self.obj("Group", new_prefab.root_only("Group").root())
        store.tag_object(group, store.guid_of(group), [])
        self.assertTrue(selection_prefab.can_select(raw))
        self.assertFalse(selection_prefab.can_select(group))
        self.assertFalse(selection_prefab.can_select(None))
        with patch.object(store, "read_state", side_effect=AssertionError("UI disk read")):
            self.assertTrue(selection_prefab.can_select(raw))
        self.assertFalse(self.prepare([raw]).has_references)
        raw.modifiers.new("Rig", "ARMATURE")
        self.assertFalse(selection_prefab.can_select(raw))
        with self.assertRaisesRegex(new_prefab.CreateError, "static mesh"):
            self.prepare([raw])
        with self.assertRaisesRegex(new_prefab.CreateError, "Object Mode"):
            self.prepare([raw], mode="EDIT_MESH")

    def test_model_library_mesh_is_not_raw(self):
        obj = self.obj("Model mesh", mesh=True)
        self.scene.collection[meshes.SOURCE_KEY] = self.layout.resolve("model.blend")
        with self.assertRaisesRegex(new_prefab.CreateError, "existing model source"):
            self.prepare([obj])

    def test_extraction_retries_only_transient_unresolved_output(self):
        from paradise_assets import ops

        race = SimpleNamespace(returncode=1, stdout="", stderr="extract.mesh wrote a mesh, "
                               "which no asset under assets/ carries")
        success = SimpleNamespace(returncode=0, stdout="", stderr="")
        arguments = ["assets", "extract", "model.blend"]
        with patch.object(ops.host, "run_cli", side_effect=[race, success]) as run:
            ops._geometry_cli(arguments, self.layout)
            self.assertEqual(run.call_count, 2)
        with patch.object(ops.host, "run_cli", return_value=race) as run:
            with self.assertRaises(new_prefab.CreateError):
                ops._geometry_cli(arguments, self.layout)
            self.assertEqual(run.call_count, 2)
        failure = SimpleNamespace(returncode=1, stdout="", stderr="conversion failed")
        for command, result in ((arguments, failure), (["assets", "mv", "a", "b"], race)):
            with patch.object(ops.host, "run_cli", return_value=result) as run:
                with self.assertRaises(new_prefab.CreateError):
                    ops._geometry_cli(command, self.layout)
                self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SelectionPreparationTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
