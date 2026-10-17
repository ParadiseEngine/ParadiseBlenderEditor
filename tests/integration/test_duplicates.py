"""A duplicated document object gets an identity of its own (``duplicates.py``).

    blender --background --factory-startup --python tests/integration/test_duplicates.py

Every Blender copy gesture copies ID properties, ``paradise_guid`` included; the handler
re-mints the claimant it has never seen. What is pinned here: the copy keeps the original's
components, an instance's overrides, and unsaved edits (a pending whole-component revert
included); the guid minted for a copy comes back when the clash is restored (what undo relies
on) but is never handed to a different object -- not after a rename, not after a delete; a
duplicated prefab CHILD stays refused; and a clash the detection cannot attribute still trips
the save's refusal.

Copies are made with ``obj.copy()`` + link, which is what Shift+D, Alt+D and Ctrl+V each do to
ID properties, and through ``bpy.ops.object.duplicate`` for the operator path.
``duplicates.settle()`` is the handler's detection-and-re-mint pass run synchronously, because
a background Blender does not pump the timer queue the handler defers through. Background
Blender also refuses ``bpy.ops.ed.undo`` (no window), so an undo across the duplicate is stood
in for by what it does to the copy: the same ID (same ``session_uid``) claims the original's
guid again.
"""

from __future__ import annotations

import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import paradise_assets
from paradise_assets import duplicates
from paradise_assets import edits as component_edits
from paradise_assets.document import prefab as prefab_document
from paradise_assets.document import project, well_known
from paradise_assets.materialize import load, save, store

failures: list[str] = []

LEVEL_GUID = "bbbbbbbb-0000-4000-8000-000000000001"
CRATE = "bbbbbbbb-0000-4000-8000-000000000002"
INSTANCE = "410f381b-fc6e-5a66-a70a-698972a199b5"
PREFAB_GUID = "5f2a1111-2222-4333-8444-555555555555"
ROOT_LOCAL = "aaaaaaaa-0000-4000-8000-000000000001"
CHILD_LOCAL = "aaaaaaaa-0000-4000-8000-000000000002"
TAG = "01b792a0-f12e-4fe9-9867-907ae988b301"
EXTRA = "01b792a0-f12e-4fe9-9867-907ae988b302"


def check(condition: bool, label: str) -> bool:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)
    return condition


def meta(body: str) -> str:
    return f'\n[[objects.components]]\nid = "{well_known.META_ID}"\ntype = "meta"\n' + body


def transform(x: float = 0.0, y: float = 0.0, z: float = 0.0) -> str:
    return (
        f'\n[[objects.components]]\nid = "{well_known.TRANSFORM_ID}"\ntype = "transform"\n'
        f"Position = [{x}, {y}, {z}]\nRotation = [0.0, 0.0, 0.0, 1.0]\nScale = [1.0, 1.0, 1.0]\n"
    )


def make_project(work: str) -> str:
    """A level with an ordinary child (with a component) and one instance that OVERRIDES a
    field of the prefab's root."""
    assets = os.path.join(work, "assets")
    os.makedirs(os.path.join(assets, "prefabs"))
    os.makedirs(os.path.join(assets, "scenes"))
    with open(os.path.join(assets, "project.toml"), "w", encoding="utf-8") as handle:
        handle.write('schema_version = 1\nname = "duptest"\n')

    prefab_path = os.path.join(assets, "prefabs", "lamp.prefab")
    with open(prefab_path, "w", encoding="utf-8") as handle:
        handle.write(
            "schema_version = 1\n"
            "\n[[objects]]\n" + meta(f'Guid = "{ROOT_LOCAL}"\nName = "Post"\n') + transform()
            + f'\n[[objects.components]]\nid = "{TAG}"\ntype = "Game.Tag"\n'
            'Value = 1\nColour = "red"\n'
            "\n[[objects]]\n"
            + meta(f'Guid = "{CHILD_LOCAL}"\nName = "Bulb"\nParent = "{ROOT_LOCAL}"\n')
            + transform()
        )
    with open(prefab_path + ".meta", "w", encoding="utf-8") as handle:
        handle.write(f'schema_version = 1\nguid = "{PREFAB_GUID}"\n')

    level = os.path.join(assets, "scenes", "dupe.prefab")
    with open(level, "w", encoding="utf-8") as handle:
        handle.write(
            "schema_version = 1\n"
            "\n[[objects]]\n" + meta(f'Guid = "{LEVEL_GUID}"\nName = "Level"\n') + transform()
            + "\n[[objects]]\n"
            + meta(f'Guid = "{CRATE}"\nName = "Crate"\nParent = "{LEVEL_GUID}"\n')
            + transform(1.0, 2.0, 3.0)
            + f'\n[[objects.components]]\nid = "{TAG}"\ntype = "Game.Tag"\n'
            'Value = 5\nColour = "green"\n'
            "\n[[objects]]\n"
            + f'prefab = {{ guid = "{PREFAB_GUID}", path = "prefabs/lamp.prefab" }}\n'
            + meta(f'Guid = "{INSTANCE}"\nName = "Lamp_03"\nParent = "{LEVEL_GUID}"\n')
            + transform(5.0)
            # An instance ADDITION: a component the prefab does not have.
            + f'\n[[objects.components]]\nid = "{EXTRA}"\ntype = "Game.Extra"\nWeight = 2\n'
            # The instance's override of the prefab root's Tag: only the touched field.
            + f'\n[[objects.components]]\nid = "{TAG}"\ntype = "Game.Tag"\nValue = 3\n'
        )
    return level


def opened(level: str):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    layout = project.locate(level)
    with open(level, encoding="utf-8") as handle:
        document = prefab_document.loads(handle.read(), level)
    load.load_document(bpy.context.scene, document, level, layout)
    return layout


def read(level: str):
    with open(level, encoding="utf-8") as handle:
        return prefab_document.loads(handle.read(), level)


def duplicate(obj):
    """What every copy gesture does to ID properties: copy, then link."""
    copy = obj.copy()
    bpy.context.scene.collection.objects.link(copy)
    return copy


def operator_duplicate(obj):
    """Shift+D through the operator: the copy is left active."""
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.duplicate()
    return bpy.context.view_layer.objects.active


def payload(obj, component_id: str) -> dict:
    found = next(
        (c.get("data") for c in store.component_json(obj)
         if isinstance(c, dict) and str(c.get("id", "")).lower() == component_id.lower()),
        None,
    )
    return found if isinstance(found, dict) else {}


def main() -> int:
    paradise_assets.register()
    try:
        with tempfile.TemporaryDirectory() as root:
            level = make_project(root)
            opened(level)
            scene = bpy.context.scene

            print("== duplicating a document object re-mints the copy ==")
            crate = store.object_with_guid(scene, CRATE)
            component_edits.set_field(crate, TAG, "Value", 7)   # unsaved edit to come along
            # The pass the depsgraph handler runs continuously, seeded before the copy exists:
            # it is what tells the original from the claimant afterwards.
            duplicates.settle()
            copy = duplicate(crate)
            check(store.guid_of(copy) == CRATE, "the copy arrives claiming the original's guid")
            duplicates.settle()
            reminted = store.guid_of(copy)
            check(reminted is not None and reminted != CRATE,
                  "the copy was given an identity of its own")
            check(store.guid_of(crate) == CRATE, "and the original kept its own")
            check(store.document_name(copy) == "Crate",
                  "the copy inherits the authored name, not Blender's 'Crate.001'")
            duplicates.settle()
            check(store.guid_of(copy) == reminted, "a repeated pass mints nothing further")

            print("\n== the save writes both as separate, fully-equipped entries ==")
            save.save_prefab(scene)
            written = read(level)
            crate_entries = [e for e in written.objects if e.name == "Crate"]
            check(len(crate_entries) == 2, "the document holds both crates")
            check({e.guid for e in crate_entries} == {CRATE, reminted},
                  "one entry per identity")
            added = next((e for e in crate_entries if e.guid == reminted), None)
            check(added is not None and added.parent == LEVEL_GUID,
                  "the copy hangs where the original hangs")
            check(added is not None
                  and list(added.component(well_known.TRANSFORM_ID).data["Position"])
                  == [1.0, 2.0, 3.0],
                  "and stands where the duplicate stood")
            check(added is not None and added.component(TAG) is not None
                  and added.component(TAG).data.get("Value") == 7
                  and added.component(TAG).data.get("Colour") == "green",
                  "the copy keeps the original's components, pending edits included")
            original_entry = next(e for e in crate_entries if e.guid == CRATE)
            check(original_entry.component(TAG).data.get("Value") == 7,
                  "and the original's own pending edit still applied")

            print("\n== duplicating a prefab instance keeps its overrides ==")
            instance = store.object_with_guid(scene, INSTANCE)
            check(payload(instance, TAG).get("Value") == 3,
                  "the instance shows its saved override")
            check(payload(instance, EXTRA).get("Weight") == 2,
                  "and its saved addition")
            component_edits.set_field(instance, TAG, "Colour", "blue")   # pending, unsaved
            component_edits.revert_field(instance, EXTRA, "")   # pending: hand the addition back
            duplicates.settle()
            second = duplicate(instance)
            duplicates.settle()
            second_guid = store.guid_of(second)
            check(second_guid is not None and second_guid != INSTANCE,
                  "the second instance has its own identity")
            check(store.prefab_of(second) == (PREFAB_GUID, "prefabs/lamp.prefab"),
                  "and instances the same prefab")
            check(payload(second, TAG).get("Value") == 3,
                  "the copy still shows the original's override")
            check(store.resolved_children(second) == set(),
                  "with no resolved children recorded as shown")
            check(not store.authors(second, TAG),
                  "authoring nothing yet, like a placed instance")

            save.save_prefab(scene)
            written = read(level)
            instances = {e.guid: e for e in written.objects
                         if e.prefab is not None and e.prefab.guid.lower() == PREFAB_GUID}
            check(set(instances) == {INSTANCE, second_guid},
                  "the document holds two instances of the one prefab")
            check(instances[second_guid].component(TAG) is not None
                  and instances[second_guid].component(TAG).data
                  == {"Value": 3, "Colour": "blue"},
                  "the copy's entry carries the override, saved and pending fields alike")
            check(instances[INSTANCE].component(TAG).data == {"Value": 3, "Colour": "blue"},
                  "and the original gained its pending edit, nothing more")
            check(not any(e.dropped for e in written.objects),
                  "and no child was written dropped for the copy")
            check(instances[second_guid].component(EXTRA) is None,
                  "the original's pending revert of its addition rides along: the copy drops it")
            check(instances[INSTANCE].component(EXTRA) is None,
                  "as the original does")

            print("\n== a minted guid belongs to one copy: undo restores it, nothing reuses it ==")
            opened(level)
            scene = bpy.context.scene
            duplicates.settle()
            crate = store.object_with_guid(scene, CRATE)

            first_copy = operator_duplicate(crate)
            first_name = first_copy.name
            duplicates.settle()
            first_guid = store.guid_of(first_copy)
            check(first_copy is not crate and first_guid is not None and first_guid != CRATE,
                  "the operator's duplicate was re-minted")

            # Undo across the duplicate: the same ID claims the original's identity again.
            first_copy[store.GUID_KEY] = CRATE
            duplicates.settle()
            check(store.guid_of(first_copy) == first_guid,
                  "a restored clash re-mints the guid the copy already had")
            check(store.guid_of(crate) == CRATE, "and the original keeps its own")

            save.save_prefab(scene)
            first_copy.name = "Barrel"   # the author renames the copy, then duplicates again
            duplicates.settle()
            second_copy = operator_duplicate(crate)
            duplicates.settle()
            check(second_copy.name == first_name,
                  "the next duplicate reuses the freed Blender name")
            second_copy_guid = store.guid_of(second_copy)
            check(second_copy_guid not in (None, CRATE, first_guid),
                  "but is minted an identity of its own")
            check(store.guid_of(first_copy) == first_guid,
                  "and the renamed, saved copy keeps the identity the document holds")

            bpy.data.objects.remove(first_copy, do_unlink=True)
            save.save_prefab(scene)
            check(read(level).by_guid().get(first_guid) is None,
                  "a deleted copy leaves the document")
            third_copy = operator_duplicate(crate)
            duplicates.settle()
            check(store.guid_of(third_copy) not in (None, CRATE, first_guid, second_copy_guid),
                  "and a later duplicate does not bring its identity back")

            print("\n== duplicating a resolved prefab child is still refused ==")
            bulb = next(o for o in scene.collection.all_objects if store.is_derived(o))
            stray = duplicate(bulb)
            duplicates.settle()
            check(store.guid_of(stray) == store.guid_of(bulb),
                  "the child's copy keeps claiming the prefab's child")
            try:
                save.save_prefab(scene)
                check(False, "the save refuses two claims on one prefab child")
            except save.SaveError as error:
                check("prefab's child" in str(error), "the save refuses two claims on one "
                                                      "prefab child")
            bpy.data.objects.remove(stray, do_unlink=True)

            print("\n== a clash the detection cannot attribute stays refused ==")
            foreign = "99999999-0000-4000-8000-000000000099"
            first = bpy.data.objects.new("Claim_A", None)
            second_claim = bpy.data.objects.new("Claim_B", None)
            scene.collection.objects.link(first)
            scene.collection.objects.link(second_claim)
            store.tag_object(first, foreign, [])
            store.tag_object(second_claim, foreign, [])   # both at once: no known original
            duplicates.settle()
            check(store.guid_of(first) == foreign and store.guid_of(second_claim) == foreign,
                  "an unattributable clash is left alone")
            try:
                save.save_prefab(scene)
                check(False, "the save refuses the clash")
            except save.SaveError as error:
                check("share one identity" in str(error), "the save refuses the clash")
            bpy.data.objects.remove(second_claim, do_unlink=True)
            first.parent = store.object_with_guid(scene, LEVEL_GUID)
            duplicates.settle()
            save.save_prefab(scene)
            check(read(level).by_guid().get(foreign) is not None,
                  "and the survivor saves once the clash is gone")
    finally:
        paradise_assets.unregister()

    print()
    if failures:
        print(f"{len(failures)} FAILED:")
        for label in failures:
            print(f"  - {label}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
