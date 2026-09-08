"""Authoring prefab-instance overrides, and the three gestures that end one.

    blender --background --factory-startup --python tests/integration/test_overrides.py

What is pinned here is the round trip an author actually makes: change something about an
instance or one of its children, save, and have the document say it as an OVERRIDE rather than
as a flattened copy -- then apply it to the prefab, revert it, or break the link entirely.

Its own temporary project throughout: these operators write two documents, and none of it may
touch a real checkout.
"""

from __future__ import annotations

import os
import sys
import tempfile

import addon_utils
import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from paradise_assets import edits
from paradise_assets.document import prefab as prefab_document
from paradise_assets.document import project, resolve, well_known
from paradise_assets.materialize import load, save, store

failures: list[str] = []

ROOT_LOCAL = "aaaaaaaa-0000-4000-8000-000000000001"
CHILD_LOCAL = "aaaaaaaa-0000-4000-8000-000000000002"
LEVEL_GUID = "bbbbbbbb-0000-4000-8000-000000000001"
INSTANCE = "410f381b-fc6e-5a66-a70a-698972a199b5"
PREFAB_GUID = "5f2a1111-2222-4333-8444-555555555555"
TAG = "01b792a0-f12e-4fe9-9867-907ae988b301"


def check(condition: bool, label: str) -> bool:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)
    return condition


def fresh_scene() -> bpy.types.Scene:
    """An empty scene WITHOUT ``wm.read_factory_settings``: that resets the preferences, which
    disables the addon, and every ``bpy.ops.paradise_assets.*`` call after it fails."""
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    return bpy.context.scene


def meta(body: str) -> str:
    return f'\n[[objects.components]]\nid = "{well_known.META_ID}"\ntype = "meta"\n' + body


def transform(x: float = 0.0) -> str:
    return (
        f'\n[[objects.components]]\nid = "{well_known.TRANSFORM_ID}"\ntype = "transform"\n'
        f"Position = [{x}, 0.0, 0.0]\nRotation = [0.0, 0.0, 0.0, 1.0]\nScale = [1.0, 1.0, 1.0]\n"
    )


def make_project(work: str):
    """``(level path, prefab path)`` for a project holding a two-object prefab placed once."""
    assets = os.path.join(work, "assets")
    os.makedirs(os.path.join(assets, "prefabs"))
    os.makedirs(os.path.join(assets, "scenes"))
    with open(os.path.join(assets, "project.toml"), "w", encoding="utf-8", newline="") as handle:
        handle.write('name = "probe"\nschema_version = 1\n')

    prefab_path = os.path.join(assets, "prefabs", "lamp.prefab")
    with open(prefab_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(
            "schema_version = 1\n"
            "\n[[objects]]\n" + meta(f'Guid = "{ROOT_LOCAL}"\nName = "Post"\n') + transform()
            + f'\n[[objects.components]]\nid = "{TAG}"\ntype = "Game.Tag"\n'
            'Value = 1\nColour = "red"\n'
            "\n[[objects]]\n"
            + meta(f'Guid = "{CHILD_LOCAL}"\nName = "Bulb"\nParent = "{ROOT_LOCAL}"\n')
            + transform()
        )
    with open(prefab_path + ".meta", "w", encoding="utf-8", newline="") as handle:
        handle.write(f'schema_version = 1\nguid = "{PREFAB_GUID}"\n')

    level = os.path.join(assets, "scenes", "lit.prefab")
    with open(level, "w", encoding="utf-8", newline="") as handle:
        handle.write(
            "schema_version = 1\n"
            "\n[[objects]]\n" + meta(f'Guid = "{LEVEL_GUID}"\nName = "Level"\n') + transform()
            + "\n[[objects]]\n"
            + f'prefab = {{ guid = "{PREFAB_GUID}", path = "prefabs/lamp.prefab" }}\n'
            + meta(f'Guid = "{INSTANCE}"\nName = "Lamp_03"\nParent = "{LEVEL_GUID}"\n')
            + transform(5.0)
        )
    return level, prefab_path


def opened(level: str):
    layout = project.locate(level)
    with open(level, encoding="utf-8") as handle:
        document = prefab_document.loads(handle.read(), level)
    load.load_document(fresh_scene(), document, level, layout)
    return layout


def read(path: str):
    with open(path, encoding="utf-8") as handle:
        return prefab_document.loads(handle.read(), path)


def instance_object():
    return store.object_with_guid(bpy.context.scene, INSTANCE)


def child_object():
    return next(o for o in bpy.context.scene.collection.all_objects if store.is_derived(o))


def main() -> int:
    addon_utils.enable("paradise_assets", default_set=True, persistent=False)
    bpy.context.preferences.addons["paradise_assets"].preferences.auto_watch = False

    print("\n== an edit to an inherited component becomes an override, not a copy ==")
    with tempfile.TemporaryDirectory() as work:
        level, prefab_path = make_project(work)
        opened(level)

        instance = instance_object()
        shown = next(c for c in store.component_json(instance) if c["id"] == TAG)
        check(shown["data"] == {"Value": 1, "Colour": "red"},
              "the panel shows the prefab's component on the instance")
        check(not store.authors(instance, TAG),
              "and knows the instance's own entry does not author it")

        edits.set_field(instance, TAG, "Value", 7)
        save.save_prefab(bpy.context.scene)

        entry = read(level).by_guid()[INSTANCE]
        override = entry.component(TAG)
        check(override is not None, "the edit is written as a component on the instance entry")
        check(override is not None and override.data == {"Value": 7},
              f"carrying ONLY the field that was touched ({override.data if override else None})")
        check(entry.prefab is not None, "and it is still an instance")

        # A copied field would shadow the prefab forever: editing `Colour` in the prefab would
        # stop reaching this instance, silently.
        check(
            override is not None and "Colour" not in override.data,
            "an untouched field is NOT copied down, so the prefab still owns it",
        )

        # The panel must still show the prefab's components after a save.
        instance = instance_object()
        after = {c["id"]: c["data"] for c in store.component_json(instance)}
        check(TAG in after and after[TAG] == {"Value": 7, "Colour": "red"},
              f"the saved instance still shows the resolved payload ({after.get(TAG)})")

    print("\n== a pending edit on a prefab's child does not outlive the save ==")
    with tempfile.TemporaryDirectory() as work:
        level, prefab_path = make_project(work)
        opened(level)

        edits.set_field(child_object(), TAG, "Value", 3)
        check(edits.count(child_object()) == 1, "the edit is pending")
        save.save_prefab(bpy.context.scene)
        check(edits.count(child_object()) == 0,
              "the save consumed it -- an overlay it never cleared would refuse every reload")

    print("\n== deleting a previously overridden child survives reload ==")
    for carrier_first in (False, True):
        with tempfile.TemporaryDirectory() as work:
            level, prefab_path = make_project(work)
            opened(level)
            child_object().location.x += 2.0
            edits.set_field(child_object(), TAG, "Value", 3)
            save.save_prefab(bpy.context.scene)
            document = read(level)
            carrier = next(o for o in document.objects if o.target == CHILD_LOCAL)
            if carrier_first:
                document.objects.remove(carrier)
                document.objects.insert(0, carrier)
                with open(level, "w", encoding="utf-8") as handle:
                    handle.write(prefab_document.dumps(document))
            opened(level)
            bpy.data.objects.remove(child_object(), do_unlink=True)
            saved = save.save_prefab(bpy.context.scene)
            carriers = [o for o in read(level).objects if o.target == CHILD_LOCAL]
            check(len(carriers) == 1 and carriers[0].dropped,
                  f"existing carrier becomes dropped (carrier first: {carrier_first})")
            check(saved.edited == 1, "deletion is reported as an edit")
            opened(level)
            check(not any(store.is_derived(o) for o in bpy.context.scene.objects),
                  "the deleted child stays absent after reload")
            with open(level, "rb") as handle:
                before = handle.read()
            save.save_prefab(bpy.context.scene)
            with open(level, "rb") as handle:
                check(handle.read() == before, "saving a dropped carrier again changes no bytes")

    print("\n== a stale carrier is preserved when its prefab child is gone ==")
    with tempfile.TemporaryDirectory() as work:
        level, prefab_path = make_project(work)
        opened(level)
        child_object().location.x += 2.0
        save.save_prefab(bpy.context.scene)
        document = read(prefab_path)
        document.objects = [o for o in document.objects if o.guid != CHILD_LOCAL]
        with open(prefab_path, "w", encoding="utf-8") as handle:
            handle.write(prefab_document.dumps(document))
        opened(level)
        with open(level, "rb") as handle:
            before = handle.read()
        save.save_prefab(bpy.context.scene)
        with open(level, "rb") as handle:
            check(handle.read() == before, "an unmaterialized stale carrier is unchanged")

    print("\n== reverting an override that is already in the file ==")
    with tempfile.TemporaryDirectory() as work:
        level, prefab_path = make_project(work)
        opened(level)
        with open(level, "rb") as handle:
            original = handle.read()

        edits.set_field(instance_object(), TAG, "Value", 7)
        save.save_prefab(bpy.context.scene)

        # Forgetting a PENDING edit would do nothing now; the override is on disk.
        edits.revert_field(instance_object(), TAG, "Value")
        save.save_prefab(bpy.context.scene)
        with open(level, "rb") as handle:
            check(handle.read() == original,
                  "revert-to-prefab removes the override from the file, not just the overlay")

    print("\n== Apply Overrides writes the prefab and clears the instance ==")
    with tempfile.TemporaryDirectory() as work:
        level, prefab_path = make_project(work)
        opened(level)

        edits.set_field(instance_object(), TAG, "Value", 7)
        child_object().location.x += 2.0
        save.save_prefab(bpy.context.scene)

        bpy.context.view_layer.objects.active = instance_object()
        check(bpy.ops.paradise_assets.apply_overrides.poll(),
              "Apply polls true on an instance that overrides something")
        result = bpy.ops.paradise_assets.apply_overrides("EXEC_DEFAULT")
        check(result == {"FINISHED"}, f"the operator finished ({result})")

        prefab = read(prefab_path)
        check(prefab.root().component(TAG).data == {"Value": 7, "Colour": "red"},
              f"the prefab took the field ({prefab.root().component(TAG).data})")
        moved = prefab.by_guid()[CHILD_LOCAL].component(well_known.TRANSFORM_ID)
        check(moved.data["Position"][0] != 0.0,
              f"and the child's new placement ({moved.data['Position']})")
        check(prefab.root().component(well_known.TRANSFORM_ID).data["Position"] == [0.0, 0.0, 0.0],
              "but NOT the instance's own placement, which belongs to the level")

        after = read(level)
        entry = after.by_guid()[INSTANCE]
        check([c.id for c in entry.components] == [well_known.META_ID, well_known.TRANSFORM_ID],
              "the instance overrides nothing any more")
        check([o.target for o in after.objects if o.target] == [], "and carries no carriers")
        check(entry.component(well_known.TRANSFORM_ID).data["Position"] == [5.0, 0.0, 0.0],
              "while keeping where it stands in the level")
        check(not bpy.ops.paradise_assets.apply_overrides.poll(),
              "Apply stops offering itself once there is nothing to apply")

    print("\n== Revert Instance throws the overrides away ==")
    with tempfile.TemporaryDirectory() as work:
        level, prefab_path = make_project(work)
        opened(level)
        with open(level, "rb") as handle:
            original = handle.read()
        with open(prefab_path, "rb") as handle:
            prefab_before = handle.read()

        edits.set_field(instance_object(), TAG, "Value", 7)
        child_object().location.x += 2.0
        save.save_prefab(bpy.context.scene)

        bpy.context.view_layer.objects.active = instance_object()
        result = bpy.ops.paradise_assets.revert_instance("EXEC_DEFAULT")
        check(result == {"FINISHED"}, f"the operator finished ({result})")
        with open(level, "rb") as handle:
            check(handle.read() == original, "the level is back to what it was")
        with open(prefab_path, "rb") as handle:
            check(handle.read() == prefab_before, "and the prefab was never touched")

    print("\n== Unpack breaks the link and keeps every identity ==")
    with tempfile.TemporaryDirectory() as work:
        level, prefab_path = make_project(work)
        opened(level)

        edits.set_field(instance_object(), TAG, "Value", 7)
        save.save_prefab(bpy.context.scene)

        before = {
            store.guid_of(o): tuple(round(v, 5) for row in o.matrix_world for v in row)
            for o in bpy.context.scene.collection.all_objects
            if store.guid_of(o) is not None
        }

        bpy.context.view_layer.objects.active = instance_object()
        check(bpy.ops.paradise_assets.unpack_instance.poll(), "Unpack polls true on an instance")
        result = bpy.ops.paradise_assets.unpack_instance("EXEC_DEFAULT")
        check(result == {"FINISHED"}, f"the operator finished ({result})")

        after = read(level)
        check(all(entry.prefab is None for entry in after.objects),
              "nothing instances the prefab any more")
        minted = resolve.mint_child_guid(INSTANCE, CHILD_LOCAL)
        check(set(after.by_guid()) == {LEVEL_GUID, INSTANCE, minted},
              f"every identity survived, the minted child included ({sorted(after.by_guid())})")
        check(after.by_guid()[INSTANCE].component(TAG).data == {"Value": 7, "Colour": "red"},
              "the override was baked in rather than reverted")

        now = {
            store.guid_of(o): tuple(round(v, 5) for row in o.matrix_world for v in row)
            for o in bpy.context.scene.collection.all_objects
            if store.guid_of(o) is not None
        }
        check(before == now, "and nothing moved on screen")
        check(not any(store.is_derived(o) for o in bpy.context.scene.collection.all_objects),
              "no object is a prefab's child any more")

        bpy.context.view_layer.objects.active = store.object_with_guid(bpy.context.scene, INSTANCE)
        check(not bpy.ops.paradise_assets.unpack_instance.poll(),
              "and Unpack no longer offers itself")

    print("\n== the gestures refuse what they cannot do ==")
    with tempfile.TemporaryDirectory() as work:
        level, prefab_path = make_project(work)
        opened(level)

        bpy.context.view_layer.objects.active = store.object_with_guid(
            bpy.context.scene, LEVEL_GUID)
        check(not bpy.ops.paradise_assets.unpack_instance.poll(),
              "a plain object has no link to break")
        check(not bpy.ops.paradise_assets.apply_overrides.poll(),
              "and nothing to apply")

        # A prefab's CHILD answers with the instance it belongs to, so clicking a bulb and
        # unpacking means the lamp.
        bpy.context.view_layer.objects.active = child_object()
        check(bpy.ops.paradise_assets.unpack_instance.poll(),
              "a prefab's child offers the gesture on behalf of its instance")

        # These gestures rewrite the FILE, so they offer themselves for what the file says. An
        # override that is still only a pending edit is not one yet.
        edits.set_field(instance_object(), TAG, "Value", 7)
        bpy.context.view_layer.objects.active = instance_object()
        check(not bpy.ops.paradise_assets.revert_instance.poll(),
              "a pending edit is not yet an override to revert")

        save.save_prefab(bpy.context.scene)
        bpy.context.view_layer.objects.active = instance_object()
        check(bpy.ops.paradise_assets.revert_instance.poll(),
              "once saved, it is")

        edits.set_field(instance_object(), TAG, "Colour", "blue")
        try:
            bpy.ops.paradise_assets.revert_instance("EXEC_DEFAULT")
            check(False, "unsaved work is refused")
        except RuntimeError as error:
            check("Save to the prefab document first" in str(error),
                  f"unsaved work is refused, and says what to do: {error}")

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
