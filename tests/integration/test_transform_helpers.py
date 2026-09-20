"""Native edit/save/reload coverage for world-placement component handles."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile

import bpy
from mathutils import Matrix, Quaternion, Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import paradise_assets
from paradise_assets import component_ops, edits
from paradise_assets.document import axes, project
from paradise_assets.document import prefab as prefab_document
from paradise_assets.document.asset_reference import AssetReference
from paradise_assets.document.prefab import PrefabComponent, PrefabDocument, PrefabObject
from paradise_assets.document.well_known import META_ID, TRANSFORM_ID
from paradise_assets.materialize import load, save, store, transform_helpers

ROOT = "aaaaaaaa-1111-4111-8111-111111111111"
OWNER = "bbbbbbbb-2222-4222-8222-222222222222"
TARGET = "cccccccc-3333-4333-8333-333333333333"
MARKER = "b6c7e010-577b-475c-ae94-7951b00f8558"
PLACEMENT = {"Position": [7.123456789, 4, -9], "Rotation": [0, 1, 0, 0],
             "Scale": [1, 2, 3], "Unknown": "preserved"}
UNASSIGNED = {"Position": [9, 8, 7], "Rotation": [0, 0, 0, 1], "Scale": [0, 0, 0]}
FIELDS = [{"name": "Position", "type": "vector3"},
          {"name": "Rotation", "type": "quaternion"}, {"name": "Scale", "type": "vector3"}]
SCHEMA = {"components": [{"id": MARKER, "type": "Game.TransportTrigger", "fields": [
    {"name": name, "type": "object", "authoredBy": "transform", "fields": FIELDS}
    for name in ("Destination", "Optional", "Unassigned")
]}]}
failures = []


def check(condition, label):
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)


def close(a, b, epsilon=1e-5):
    return len(a) == len(b) and all(abs(x - y) < epsilon for x, y in zip(a, b, strict=True))


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write(path, document):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(prefab_document.dumps(document))


def entry(guid, name, parent=None, position=(0, 0, 0), scale=(1, 1, 1)):
    meta = {"Guid": guid, "Name": name}
    if parent:
        meta["Parent"] = parent
    return PrefabObject(components=[
        PrefabComponent(META_ID, "meta", meta),
        PrefabComponent(TRANSFORM_ID, "transform", {
            "Position": list(position), "Rotation": [0, 0, 0, 1], "Scale": list(scale)})])


def payload(path, name="Door"):
    document = prefab_document.loads(read(path), path)
    return next(o for o in document.objects if o.name == name).component(MARKER).data


def named(name):
    return next(o for o in bpy.context.scene.collection.all_objects if store.document_name(o) == name)


def open_document(path, layout):
    return load.load_document(bpy.context.scene, prefab_document.loads(read(path), path), path, layout)


def main():
    paradise_assets.register()
    try:
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "assets", "levels"))
            os.makedirs(os.path.join(root, ".editor"))
            with open(os.path.join(root, "assets", "project.toml"), "w", encoding="utf-8") as handle:
                handle.write('schema_version = 1\nname = "transformtest"\n')
            schema_path = os.path.join(root, ".editor", "authoring-schema.json")
            with open(schema_path, "w", encoding="utf-8") as handle:
                json.dump(SCHEMA, handle)
            path = os.path.join(root, "assets", "levels", "test.prefab")
            door = entry(OWNER, "Door", ROOT, position=(2, 3, 4))
            door.components.append(PrefabComponent(MARKER, "Game.TransportTrigger", {
                "Destination": copy.deepcopy(PLACEMENT), "Unassigned": copy.deepcopy(UNASSIGNED),
                "Untouched": [8, 9, 10]}))
            document = PrefabDocument(objects=[
                entry(ROOT, "Level", position=(10, 20, 30), scale=(2, 3, 4)), door,
                entry(TARGET, "Landing", ROOT, position=(1, 2, 3), scale=(3, 4, 5))])
            write(path, document)
            layout = project.locate(path)
            bpy.ops.wm.read_factory_settings(use_empty=True)
            before = read(path)
            open_document(path, layout)
            owner = named("Door")
            handle = transform_helpers.helper_for(owner, MARKER, "Destination")
            expected = Matrix(axes.to_blender(axes.trs_to_matrix(
                PLACEMENT["Position"], PLACEMENT["Rotation"], PLACEMENT["Scale"])))
            check(handle is not None and all(
                close(a, b) for a, b in zip(handle.matrix_world, expected, strict=True)),
                  "destination uses authored world placement under scaled, translated parents")
            check(transform_helpers.helper_for(owner, MARKER, "Optional") is None
                  and transform_helpers.helper_for(owner, MARKER, "Unassigned") is None,
                  "absent and zero-scale unassigned payloads create no handles")
            check(store.guid_of(handle) is None, "handles have no document identity")
            bpy.context.view_layer.objects.active = handle
            check(component_ops.document_object(bpy.context) is owner,
                  "selecting a handle retains its owner's component panel")
            save.save_prefab(bpy.context.scene)
            check(read(path) == before, "untouched save preserves every byte")

            world = handle.matrix_world.copy()
            world.translation += Vector((2, -5, 3))
            handle.matrix_world = world
            save.save_prefab(bpy.context.scene)
            written = payload(path)
            check(close(written["Destination"]["Position"], (9.123456789, 7, -4)),
                  "moving the world handle exports document-space XYZ")
            check(written["Destination"]["Rotation"] == PLACEMENT["Rotation"]
                  and written["Destination"]["Scale"] == PLACEMENT["Scale"]
                  and written["Destination"]["Unknown"] == "preserved",
                  "unchanged orientation, scale and unknown members retain their authored values")
            check("Optional" not in written and written["Unassigned"] == UNASSIGNED
                  and written["Untouched"] == [8, 9, 10], "unrelated and unassigned values stay untouched")
            moved = read(path)
            save.save_prefab(bpy.context.scene)
            check(read(path) == moved, "second save after an edit is byte-identical")
            open_document(path, layout)
            owner = named("Door")
            check(len([o for o in bpy.data.objects if transform_helpers.is_helper(o)]) == 1,
                  "reload sweeps previous handles without duplication")
            save.save_prefab(bpy.context.scene)
            check(read(path) == moved, "edited placement survives reload without drift")

            target = named("Landing")
            target.rotation_mode = "QUATERNION"
            target.rotation_quaternion = Quaternion((0, 0, 1), 0.75)
            bpy.context.view_layer.update()
            expected_position, expected_rotation, expected_scale = Matrix(
                axes.to_document([list(row) for row in target.matrix_world])).decompose()
            bpy.context.view_layer.objects.active = owner
            outcome = bpy.ops.paradise_assets.transform_slot(
                action="PICK", owner_guid=OWNER, component_id=MARKER, field_name="Destination",
                target_name=target.name)
            check(outcome == {"FINISHED"}, "object picker assigns a placement handle")
            save.save_prefab(bpy.context.scene)
            picked = payload(path)["Destination"]
            check(close(picked["Position"], expected_position)
                  and close(picked["Scale"], expected_scale)
                  and abs(Quaternion((picked["Rotation"][3], *picked["Rotation"][:3]))
                          .dot(expected_rotation.normalized())) > 1 - 1e-5,
                  "picker exports the source object's world position, rotation and lossy scale")

            handle = transform_helpers.helper_for(owner, MARKER, "Destination")
            duplicate = handle.copy()
            bpy.context.scene.collection.objects.link(duplicate)
            before_duplicate = read(path)
            try:
                save.save_prefab(bpy.context.scene)
                check(False, "duplicate handles cannot overwrite one destination unpredictably")
            except save.SaveError as error:
                check("duplicate transform handles" in str(error) and read(path) == before_duplicate,
                      "duplicate handles refuse save before changing the document")
            bpy.data.objects.remove(duplicate, do_unlink=True)

            handle = transform_helpers.helper_for(owner, MARKER, "Destination")
            bpy.data.objects.remove(handle, do_unlink=True)
            save.save_prefab(bpy.context.scene)
            check("Destination" not in payload(path), "deleting the handle clears its authored field")
            bpy.context.view_layer.objects.active = owner
            created = bpy.ops.paradise_assets.transform_slot(
                action="CREATE", owner_guid=OWNER, component_id=MARKER, field_name="Destination")
            check(created == {"FINISHED"},
                "an absent field can be assigned explicitly")
            save.save_prefab(bpy.context.scene)
            check("Destination" in payload(path), "explicit creation writes a valid placement")

            handle = transform_helpers.helper_for(owner, MARKER, "Destination")
            target.parent = handle
            try:
                save.save_prefab(bpy.context.scene)
                check(False, "document objects cannot be parented under transform handles")
            except save.SaveError as error:
                check("parent must be a document object" in str(error)
                      and store.guid_of(handle) is None,
                      "transform handle is refused as a parent rather than adopted as a group")

            # The prefab root and a resolved child both inherit a transform field.
            prop_path = os.path.join(root, "assets", "levels", "door.prefab")
            prop = copy.deepcopy(document)
            prop.objects = prop.objects[:2]
            prop.objects[0].components.append(PrefabComponent(MARKER, "Game.TransportTrigger", {
                "Destination": copy.deepcopy(PLACEMENT), "Untouched": 123}))
            write(prop_path, prop)
            with open(prop_path + ".meta", "w", encoding="utf-8") as meta:
                meta.write(f'schema_version = 1\nguid = "{ROOT}"\n')
            instance = entry(TARGET, "Instance")
            instance.prefab = AssetReference(ROOT, "levels/door.prefab")
            placed_path = os.path.join(root, "assets", "levels", "placed.prefab")
            write(placed_path, PrefabDocument(objects=[instance]))
            before = read(placed_path)
            open_document(placed_path, layout)
            save.save_prefab(bpy.context.scene)
            check(read(placed_path) == before, "untouched inherited placements do not create overrides")
            instance_obj = named("Instance")
            root_handle = transform_helpers.helper_for(instance_obj, MARKER, "Destination")
            child_obj = named("Door")
            child_handle = transform_helpers.helper_for(child_obj, MARKER, "Destination")
            root_handle.location.x += 2
            child_handle.location.z += 4
            save.save_prefab(bpy.context.scene)
            placed = prefab_document.loads(read(placed_path), placed_path)
            check(len(placed.objects) == 2, "editing a child creates one override carrier")
            root_override = placed.objects[0].component(MARKER).data
            child_override = next(o for o in placed.objects if o.target is not None).component(MARKER).data
            check(set(root_override) == {"Destination"} and set(child_override) == {"Destination"},
                  "moving inherited handles overrides only their placement fields")
            check(close(root_override["Destination"]["Position"], (9.123456789, 4, -9))
                  and close(child_override["Destination"]["Position"], (7.123456789, 8, -9)),
                  "instance and child placements both bake world movement")
            again = read(placed_path)
            save.save_prefab(bpy.context.scene)
            check(read(placed_path) == again, "saved inherited edits remain stable")
            edits.revert_field(instance_obj, MARKER, "Destination")
            edits.revert_field(child_obj, MARKER, "Destination")
            save.save_prefab(bpy.context.scene)
            check(read(placed_path) == before, "revert-to-prefab removes both transform overrides")
            check(close(root_handle.matrix_world.translation, expected.translation),
                  "reverting restores the helper to the inherited placement")
            bpy.data.objects.remove(root_handle, do_unlink=True)
            save.save_prefab(bpy.context.scene)
            check(payload(placed_path, "Instance")["Destination"] == {},
                  "clearing an inherited placement writes an unassigned field override")
            open_document(placed_path, layout)
            check(transform_helpers.helper_for(named("Instance"), MARKER, "Destination") is None,
                  "cleared inherited placement stays unassigned after reload")

            open_document(path, layout)
            owner = named("Door")
            handle = transform_helpers.helper_for(owner, MARKER, "Destination")
            bpy.context.view_layer.objects.active = handle
            check(bpy.ops.paradise_assets.remove_component(component_id=MARKER) == {"FINISHED"}
                  and MARKER in edits.removed_ids(owner),
                  "removing a component with its handle selected edits the document owner")
            save.save_prefab(bpy.context.scene)
            check(transform_helpers.helper_for(owner, MARKER, "Destination") is None
                  and edits.count(owner) == 0,
                  "component removal saves and cleans helpers without stale object references")
            write(path, document)
            open_document(path, layout)
            owner = named("Door")
            edits.set_field(owner, MARKER, "Destination", {})
            save.save_prefab(bpy.context.scene)
            check(payload(path)["Destination"] == {}
                  and transform_helpers.helper_for(owner, MARKER, "Destination") is None,
                  "explicit empty placement saves and removes its former helper")
            write(path, document)
            open_document(path, layout)
            bpy.data.objects.remove(named("Door"), do_unlink=True)
            save.save_prefab(bpy.context.scene)
            check(not any(transform_helpers.is_helper(o) for o in bpy.context.scene.collection.all_objects),
                  "removing the owner removes its orphaned placement helper")

            large = PrefabDocument(objects=[entry(ROOT, "Large scene")] + [
                entry(f"{i:08x}-4444-4444-8444-444444444444", f"Object {i}", ROOT)
                for i in range(3999)
            ])
            large_path = os.path.join(root, "assets", "levels", "large.prefab")
            write(large_path, large)
            before = read(large_path)
            opened = open_document(large_path, layout)
            saved = save.save_prefab(bpy.context.scene)
            check(opened.objects == saved.written == 4000 and read(large_path) == before,
                  "a 4000-object scene without transform handles loads and saves byte-identically")
    finally:
        paradise_assets.unregister()
    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
