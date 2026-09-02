"""Opening a scene document in Blender, and writing it back unchanged.

    blender --background --factory-startup --python tests/integration/test_open_scene.py -- <project>

``<project>`` is a directory holding ``assets/project.toml``; it defaults to the workspace's
ShiningPie checkout, because the point of this test is real content -- 225 objects across three
documents, referencing 117 GLBs.

THE test is the round trip: open a document, save it straight back, and require the bytes to be
identical. Nothing else proves the two halves agree. A loader that got the axis conversion wrong
in a self-consistent way would pass every unit test in the repo and fail this one, and so would
a save that rewrote every float it touched.
"""

from __future__ import annotations

import glob
import math
import os
import shutil
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from paradise_assets.document import prefab as prefab_document
from paradise_assets.document import project, well_known
from paradise_assets.materialize import instancing, load, save, store

DEFAULT_PROJECT = r"C:\proj\paradise-workspace\shiningpie"

failures: list[str] = []


def check(condition: bool, label: str) -> bool:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)
    return condition


def fresh_scene() -> bpy.types.Scene:
    """A scene with nothing in it -- so object counts mean what they say."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    return bpy.context.scene


def open_document(path: str, layout) -> load.LoadResult:
    with open(path, encoding="utf-8") as handle:
        document = prefab_document.loads(handle.read(), path)
    return load.load_document(fresh_scene(), document, path, layout)


def main() -> int:
    root = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else DEFAULT_PROJECT
    layout = project.locate(root)
    if layout is None:
        # Not a failure: this addon's tests need a real asset project, and a clone of this
        # repository alone has none. Skipping loudly beats a red suite nobody can fix here.
        print(f"SKIP: no asset project at or above {root}")
        return 0

    # Recursive, because authoring documents are no longer confined to one `scenes/` directory:
    # they are `*.prefab` anywhere under assets/, and shiningpie keeps levels and props in separate
    # folders. Same glob `catalogue.build` uses.
    documents = sorted(glob.glob(os.path.join(layout.assets, "**", "*.prefab"), recursive=True))
    check(bool(documents), f"found {len(documents)} document(s) under {layout.assets}")
    if not documents:
        # Everything past here indexes documents[0]; without this the suite dies with an
        # IndexError that says nothing about the actual problem.
        print("\n(no documents to exercise -- stopping here)")
        return 1

    print("\n== the round trip: open -> save must be byte-identical ==")
    for path in documents:
        with open(path, "rb") as handle:
            original = handle.read()
        with tempfile.TemporaryDirectory() as work:
            copy = os.path.join(work, os.path.basename(path))
            shutil.copy2(path, copy)

            result = open_document(copy, layout)
            saved = save.save_prefab(bpy.context.scene)

            with open(copy, "rb") as handle:
                check(
                    handle.read() == original,
                    f"{os.path.basename(path)}: {result.objects} objects round-trip byte for byte",
                )
            check(saved.moved == 0, f"{os.path.basename(path)}: nothing reported as moved")

    print("\n== the document is materialized faithfully ==")
    # By name rather than by a fixed path: `test.prefab` sits under `levels/` in shiningpie, and
    # which folder a project files its levels in is not this test's business.
    test_document = next(
        (p for p in documents if os.path.basename(p) == "test.prefab"), None
    )
    if test_document is not None:
        with open(test_document, encoding="utf-8") as handle:
            document = prefab_document.loads(handle.read(), test_document)
        result = open_document(test_document, layout)

        objects = [o for o in bpy.context.scene.collection.all_objects if store.guid_of(o)]
        check(len(objects) == len(document.objects), f"{len(document.objects)} objects created")
        check(
            {store.guid_of(o) for o in objects} == {e.guid for e in document.objects},
            "every document identity is present exactly once",
        )

        parented = [e for e in document.objects if e.parent]
        by_guid = {store.guid_of(o): o for o in objects}
        check(
            all(store.guid_of(by_guid[e.guid].parent) == e.parent for e in parented),
            f"{len(parented)} parent link(s) match the document",
        )

        instanced = [o for o in objects if o.instance_collection is not None]
        check(bool(instanced), f"{len(instanced)} object(s) display an imported mesh")
        check(
            len({o.instance_collection.name for o in instanced}) <= len(instanced),
            "meshes are shared between objects rather than imported per object",
        )

    print("\n== a real edit writes exactly that edit ==")
    with tempfile.TemporaryDirectory() as work:
        copy = os.path.join(work, "edit.prefab")
        shutil.copy2(documents[0], copy)
        with open(copy, encoding="utf-8") as handle:
            before = prefab_document.loads(handle.read(), copy)

        open_document(copy, layout)
        moved = next(o for o in bpy.context.scene.collection.all_objects if store.guid_of(o))
        target = store.guid_of(moved)
        moved.location.x += 5.0
        result = save.save_prefab(bpy.context.scene)

        with open(copy, encoding="utf-8") as handle:
            after = prefab_document.loads(handle.read(), copy)
        check(result.moved == 1, "exactly one object reported as moved")

        # Compared as DOCUMENTS, not as lines: giving an object its first transform inserts a
        # table, which shifts every line below it -- a textual diff would call the whole file
        # changed and say nothing about whether anything really did.
        old, new = before.by_guid(), after.by_guid()
        check(set(old) == set(new), "no object appeared or disappeared")
        def placement(entry):
            component = entry.component(well_known.TRANSFORM_ID)
            return None if component is None else component.data

        differing = [g for g in old if placement(old[g]) != placement(new[g])]
        check(differing == [target], f"exactly the moved object's transform changed ({len(differing)})")
        def payloads(entry):
            # Every component EXCEPT the transform, which is the thing the edit was.
            return [c for c in entry.components if c.id != well_known.TRANSFORM_ID]

        check(
            all(payloads(old[g]) == payloads(new[g]) for g in old),
            "no component payload changed",
        )
        check(
            all((old[g].name, old[g].parent) == (new[g].name, new[g].parent) for g in old),
            "no name or parent changed",
        )

    print("\n== the stamp refuses a save over an external change ==")
    with tempfile.TemporaryDirectory() as work:
        copy = os.path.join(work, "stale.prefab")
        shutil.copy2(documents[0], copy)
        open_document(copy, layout)

        # Rewrite it behind the addon's back, as another tool or a `git pull` would.
        with open(copy, "a", encoding="utf-8") as handle:
            handle.write("\n")

        try:
            save.save_prefab(bpy.context.scene)
            check(False, "a stale document refuses the save")
        except save.SaveError as error:
            check("changed on disk" in str(error), "a stale document refuses the save")

    print("\n== a parent that is not an entity refuses the save ==")
    with tempfile.TemporaryDirectory() as work:
        copy = os.path.join(work, "parented.prefab")
        shutil.copy2(documents[0], copy)
        open_document(copy, layout)

        # A Blender-only helper object, the way an author's rig or guide empty appears: no
        # identity, so a parent link to it has nothing to record.
        helper = bpy.data.objects.new("RigHelper", None)
        bpy.context.scene.collection.objects.link(helper)
        entity = next(o for o in bpy.context.scene.collection.all_objects if store.guid_of(o))
        entity.parent = helper

        try:
            save.save_prefab(bpy.context.scene)
            check(False, "a foreign parent refuses the save")
        except save.SaveError as error:
            check(
                "RigHelper" in str(error) and entity.name in str(error),
                "a foreign parent refuses the save, naming both objects",
            )

    print("\n== an unrecognised component survives the round trip ==")
    with tempfile.TemporaryDirectory() as work:
        copy = os.path.join(work, "unknown.prefab")
        with open(copy, "w", encoding="utf-8", newline="") as handle:
            handle.write(
                "schema_version = 1\n"
                "\n[[objects]]\n"
                "\n[[objects.components]]\n"
                f'id = "{well_known.META_ID}"\n'
                'type = "meta"\n'
                'Guid = "11111111-2222-4333-8444-555555555555"\n'
                'Name = "thing"\n'
                "\n[[objects.components]]\n"
                f'id = "{well_known.TRANSFORM_ID}"\ntype = "transform"\n'
                'Position = [0.0, 0.0, 0.0]\n'
                'Rotation = [0.0, 0.0, 0.0, 1.0]\n'
                'Scale = [1.0, 1.0, 1.0]\n'
                "\n[[objects.components]]\n"
                'id = "99999999-8888-4777-8666-555555555555"\n'
                'type = "Nobody.Has.Heard.Of.This"\n'
                "Weird = 42\n"
                'Nested = "keep me"\n'
            )
        with open(copy, "rb") as handle:
            original = handle.read()
        open_document(copy, layout)
        save.save_prefab(bpy.context.scene)
        with open(copy, "rb") as handle:
            check(handle.read() == original, "an unknown component is written back verbatim")

    print("\n== a prefab instance stays an instance through a save ==")
    with tempfile.TemporaryDirectory() as work:
        # A project of its own, so the prefab reference resolves against this tree. The manifest
        # lives under assets/ -- that is what project.locate looks for, and a bare project.toml at
        # the root finds nothing.
        assets = os.path.join(work, "assets")
        prefabs = os.path.join(assets, "prefabs")
        scenes_dir = os.path.join(assets, "scenes")
        os.makedirs(prefabs)
        os.makedirs(scenes_dir)
        with open(os.path.join(assets, "project.toml"), "w", encoding="utf-8", newline="") as handle:
            handle.write('name = "probe"\nschema_version = 1\n')

        root_local = "aaaaaaaa-0000-4000-8000-000000000001"
        child_local = "aaaaaaaa-0000-4000-8000-000000000002"
        instance_guid = "410f381b-fc6e-5a66-a70a-698972a199b5"

        prefab_path = os.path.join(prefabs, "lamp.prefab")
        with open(prefab_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(
                "schema_version = 1\n"
                "\n[[objects]]\n\n[[objects.components]]\n"
                f'id = "{well_known.META_ID}"\ntype = "meta"\nGuid = "{root_local}"\nName = "Post"\n'
                "\n[[objects.components]]\n"
                f'id = "{well_known.TRANSFORM_ID}"\ntype = "transform"\n'
                'Position = [0.0, 0.0, 0.0]\nRotation = [0.0, 0.0, 0.0, 1.0]\nScale = [1.0, 1.0, 1.0]\n'
                "\n[[objects]]\n\n[[objects.components]]\n"
                f'id = "{well_known.META_ID}"\ntype = "meta"\nGuid = "{child_local}"\n'
                f'Name = "Bulb"\nParent = "{root_local}"\n'
                "\n[[objects.components]]\n"
                f'id = "{well_known.TRANSFORM_ID}"\ntype = "transform"\n'
                'Position = [0.0, 0.0, 0.0]\nRotation = [0.0, 0.0, 0.0, 1.0]\nScale = [1.0, 1.0, 1.0]\n'
            )

        scene_path = os.path.join(scenes_dir, "lit.prefab")
        with open(scene_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(
                "schema_version = 1\n"
                "\n[[objects]]\n"
                'prefab = { guid = "5f2a1111-2222-4333-8444-555555555555", path = "prefabs/lamp.prefab" }\n'
                "\n[[objects.components]]\n"
                f'id = "{well_known.META_ID}"\ntype = "meta"\n'
                f'Guid = "{instance_guid}"\nName = "Lamp_03"\n'
                "\n[[objects.components]]\n"
                f'id = "{well_known.TRANSFORM_ID}"\ntype = "transform"\n'
                'Position = [0.0, 0.0, 0.0]\nRotation = [0.0, 0.0, 0.0, 1.0]\nScale = [1.0, 1.0, 1.0]\n'
            )

        with open(scene_path, "rb") as handle:
            original = handle.read()

        # The project root is `work`, not the shiningpie checkout.
        probe_layout = project.locate(scene_path)
        with open(scene_path, encoding="utf-8") as handle:
            document = prefab_document.loads(handle.read(), scene_path)
        result = load.load_document(fresh_scene(), document, scene_path, probe_layout)

        check(result.objects == 2, f"the instance materializes as {result.objects} objects (root + child)")
        check(result.derived == 1, "the prefab's child is marked derived")

        derived = [o for o in bpy.context.scene.collection.all_objects if store.is_derived(o)]
        check(all(all(o.lock_location) for o in derived), "derived children are locked in the viewport")

        save.save_prefab(bpy.context.scene)

        # THE check: the document still holds ONE object with a prefab reference. A save that
        # treated the resolved children as ordinary objects would have written two plain objects
        # here and lost the instance for good.
        with open(scene_path, "rb") as handle:
            check(handle.read() == original, "the instance is written back unflattened")

    print("\n== an override carrier survives a save (#26) ==")
    with tempfile.TemporaryDirectory() as work:
        assets = os.path.join(work, "assets")
        prefabs = os.path.join(assets, "prefabs")
        scenes_dir = os.path.join(assets, "scenes")
        os.makedirs(prefabs)
        os.makedirs(scenes_dir)
        with open(os.path.join(assets, "project.toml"), "w", encoding="utf-8", newline="") as handle:
            handle.write('name = "probe"\nschema_version = 1\n')

        root_local = "aaaaaaaa-0000-4000-8000-000000000001"
        child_local = "aaaaaaaa-0000-4000-8000-000000000002"
        instance_guid = "410f381b-fc6e-5a66-a70a-698972a199b5"
        second_instance = "510f381b-fc6e-5a66-a70a-698972a199b5"
        materials_id = "bdc4fc87-d7b4-41f1-bc90-fc827005adfc"

        def meta(body: str) -> str:
            return f'\n[[objects.components]]\nid = "{well_known.META_ID}"\ntype = "meta"\n' + body

        def transform() -> str:
            return (
                f'\n[[objects.components]]\nid = "{well_known.TRANSFORM_ID}"\ntype = "transform"\n'
                "Position = [0.0, 0.0, 0.0]\nRotation = [0.0, 0.0, 0.0, 1.0]\nScale = [1.0, 1.0, 1.0]\n"
            )

        with open(os.path.join(prefabs, "lamp.prefab"), "w", encoding="utf-8", newline="") as handle:
            handle.write(
                "schema_version = 1\n\n[[objects]]\n"
                + meta(f'Guid = "{root_local}"\nName = "Post"\n') + transform()
                + "\n[[objects]]\n"
                + meta(f'Guid = "{child_local}"\nName = "Bulb"\nParent = "{root_local}"\n') + transform()
                + f'\n[[objects.components]]\nid = "{materials_id}"\ntype = "Materials"\nSlots = [{{}}]\n'
            )

        # Two instances; the first carries an override on its child, the second a Dropped one.
        reference = (
            'prefab = { guid = "5f2a1111-2222-4333-8444-555555555555", path = "prefabs/lamp.prefab" }\n'
        )
        scene_text = (
            "schema_version = 1\n"
            "\n[[objects]]\n" + reference
            + meta(f'Guid = "{instance_guid}"\nName = "Lamp_03"\n') + transform()
            + "\n[[objects]]\n"
            + meta(f'Parent = "{instance_guid}"\nTarget = "{child_local}"\n')
            + f'\n[[objects.components]]\nid = "{materials_id}"\nSlots = [{{}}, {{}}]\n'
            + "\n[[objects]]\n" + reference
            + meta(f'Guid = "{second_instance}"\nName = "Lamp_04"\nParent = "{instance_guid}"\n')
            + transform()
            + "\n[[objects]]\n"
            + meta(f'Parent = "{second_instance}"\nTarget = "{child_local}"\nDropped = true\n')
        )
        scene_path = os.path.join(scenes_dir, "lit.prefab")
        with open(scene_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(scene_text)
        with open(scene_path, "rb") as handle:
            original = handle.read()

        probe_layout = project.locate(scene_path)
        with open(scene_path, encoding="utf-8") as handle:
            document = prefab_document.loads(handle.read(), scene_path)
        result = load.load_document(fresh_scene(), document, scene_path, probe_layout)
        # The probe project has no dumped schema, which the load says once; anything else is real.
        problems = [w for w in result.warnings if "authoring-schema.json" not in w]
        check(problems == [], f"the carriers resolve cleanly: {problems}")
        check(result.objects == 3, f"two instances and one overridden child materialize ({result.objects})")

        overridden = next(o for o in bpy.context.scene.collection.all_objects if store.is_derived(o))
        shown = next(c for c in store.component_json(overridden) if c["id"] == materials_id)
        slots = shown["data"]["Slots"]
        check(slots == [{}, {}], "the carrier's override reaches the displayed child")

        saved = save.save_prefab(bpy.context.scene)
        with open(scene_path, "rb") as handle:
            check(handle.read() == original, "both carriers are written back verbatim")
        check(saved.removed == 0, f"nothing is reported removed ({saved.removed})")

        # Deleting an instance takes its carrier with it; the other carrier stays.
        bpy.data.objects.remove(store.object_with_guid(bpy.context.scene, second_instance), do_unlink=True)
        saved = save.save_prefab(bpy.context.scene)
        with open(scene_path, encoding="utf-8") as handle:
            after = prefab_document.loads(handle.read(), scene_path)
        check(saved.removed == 2, f"the instance and its carrier count as removed ({saved.removed})")
        check(
            [o.target for o in after.objects] == [None, child_local],
            "the surviving instance keeps its carrier and the deleted one's is gone",
        )

        # A derived child cannot be moved: the document has no way to say so.
        overridden = next(o for o in bpy.context.scene.collection.all_objects if store.is_derived(o))
        overridden.location.x += 1.0
        try:
            save.save_prefab(bpy.context.scene)
            check(False, "moving a prefab's child is refused at save")
        except save.SaveError as error:
            check("cannot express" in str(error), f"the refusal says why: {error}")

    print("\n== an instance whose prefab is missing does not take the load down ==")
    with tempfile.TemporaryDirectory() as work:
        assets = os.path.join(work, "assets")
        levels = os.path.join(assets, "levels")
        os.makedirs(levels)
        with open(os.path.join(assets, "project.toml"), "w", encoding="utf-8", newline="") as handle:
            handle.write('name = "probe"\nschema_version = 1\n')

        root_guid = "dddddddd-0000-4000-8000-000000000001"
        broken = "dddddddd-0000-4000-8000-000000000002"
        orphan = "dddddddd-0000-4000-8000-000000000003"

        def meta(body: str) -> str:
            return f'\n[[objects.components]]\nid = "{well_known.META_ID}"\ntype = "meta"\n' + body

        text = (
            "schema_version = 1\n\n[[objects]]\n" + meta(f'Guid = "{root_guid}"\nName = "Level"\n')
            + "\n[[objects]]\n"
            'prefab = { guid = "5f2a1111-2222-4333-8444-555555555555", path = "prefabs/gone.prefab" }\n'
            + meta(f'Guid = "{broken}"\nName = "Ghost"\nParent = "{root_guid}"\n')
            + "\n[[objects]]\n" + meta(f'Guid = "{orphan}"\nName = "Lantern"\nParent = "{broken}"\n')
        )
        scene_path = os.path.join(levels, "haunted.prefab")
        with open(scene_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)

        probe_layout = project.locate(scene_path)
        with open(scene_path, encoding="utf-8") as handle:
            document = prefab_document.loads(handle.read(), scene_path)
        try:
            result = load.load_document(fresh_scene(), document, scene_path, probe_layout)
        except KeyError as error:
            check(False, f"a child of an unreadable instance raised KeyError {error}")
        else:
            check(any("could not be read" in w for w in result.warnings),
                  "the missing prefab is warned about")
            check(any("unparented" in w for w in result.warnings), "the orphaned child is warned about")
            lantern = store.object_with_guid(bpy.context.scene, orphan)
            check(lantern is not None and lantern.parent is None, "the child is shown, unparented")

    print("\n== names, rotation modes and file modes survive a save (#32, #37) ==")
    with tempfile.TemporaryDirectory() as work:
        assets = os.path.join(work, "assets")
        levels = os.path.join(assets, "levels")
        os.makedirs(levels)
        with open(os.path.join(assets, "project.toml"), "w", encoding="utf-8", newline="") as handle:
            handle.write('name = "probe"\nschema_version = 1\n')

        root_guid = "bbbbbbbb-0000-4000-8000-000000000001"
        wall_a = "bbbbbbbb-0000-4000-8000-000000000002"
        wall_b = "bbbbbbbb-0000-4000-8000-000000000003"
        nameless = "bbbbbbbb-0000-4000-8000-000000000004"

        def meta(body: str) -> str:
            return f'\n[[objects.components]]\nid = "{well_known.META_ID}"\ntype = "meta"\n' + body

        def transform() -> str:
            return (
                f'\n[[objects.components]]\nid = "{well_known.TRANSFORM_ID}"\ntype = "transform"\n'
                "Position = [0.0, 0.0, 0.0]\nRotation = [0.0, 0.0, 0.0, 1.0]\nScale = [1.0, 1.0, 1.0]\n"
            )

        # Two children share the name "Wall" (the format allows it); one object has no name.
        text = (
            "schema_version = 1\n\n[[objects]]\n"
            + meta(f'Guid = "{root_guid}"\nName = "Level"\n') + transform()
            + "\n[[objects]]\n"
            + meta(f'Guid = "{wall_a}"\nName = "Wall"\nParent = "{root_guid}"\n') + transform()
            + "\n[[objects]]\n"
            + meta(f'Guid = "{wall_b}"\nName = "Wall"\nParent = "{root_guid}"\n') + transform()
            + "\n[[objects]]\n" + meta(f'Guid = "{nameless}"\nParent = "{root_guid}"\n') + transform()
        )
        scene_path = os.path.join(levels, "walls.prefab")
        with open(scene_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.chmod(scene_path, 0o644)

        # A prop to place, with the sidecar that gives it an identity to reference.
        prop_path = os.path.join(levels, "prop.prefab")
        with open(prop_path, "w", encoding="utf-8", newline="") as handle:
            handle.write("schema_version = 1\n\n[[objects]]\n" + meta(f'Guid = "{wall_a}"\nName = "Prop"\n'))
        with open(prop_path + ".meta", "w", encoding="utf-8", newline="") as handle:
            handle.write('guid = "cccccccc-0000-4000-8000-000000000001"\n')
        with open(scene_path, "rb") as handle:
            original = handle.read()

        probe_layout = project.locate(scene_path)
        with open(scene_path, encoding="utf-8") as handle:
            document = prefab_document.loads(handle.read(), scene_path)
        load.load_document(fresh_scene(), document, scene_path, probe_layout)

        second_wall = store.object_with_guid(bpy.context.scene, wall_b)
        check(second_wall.name != "Wall", f"Blender uniquified the second Wall to '{second_wall.name}'")

        save.save_prefab(bpy.context.scene)
        with open(scene_path, "rb") as handle:
            check(handle.read() == original,
                  "Blender's .001 suffix and the missing name are not written into the document")
        check((os.stat(scene_path).st_mode & 0o777) == 0o644, "the document keeps its file mode")

        # A rename by the author IS written.
        second_wall.name = "EastWall"
        save.save_prefab(bpy.context.scene)
        with open(scene_path, encoding="utf-8") as handle:
            renamed = prefab_document.loads(handle.read(), scene_path)
        check(renamed.by_guid()[wall_b].name == "EastWall", "an author's rename reaches meta.Name")

        # AXIS_ANGLE is a rotation mode the viewport shows; the save used to read the euler.
        first_wall = store.object_with_guid(bpy.context.scene, wall_a)
        first_wall.rotation_mode = "AXIS_ANGLE"
        first_wall.rotation_axis_angle = (math.pi / 2, 0.0, 0.0, 1.0)
        saved = save.save_prefab(bpy.context.scene)
        with open(scene_path, encoding="utf-8") as handle:
            rotated = prefab_document.loads(handle.read(), scene_path)
        rotation = rotated.by_guid()[wall_a].component(well_known.TRANSFORM_ID).data["Rotation"]
        check(saved.moved == 1 and abs(abs(rotation[3]) - math.cos(math.pi / 4)) < 1e-6
              and rotation[3] >= 0.0,
              f"an AXIS_ANGLE rotation is saved as the viewport shows it, w >= 0: {rotation}")

        # Deleting the root unparents its children; the save must refuse, not write two roots.
        bpy.data.objects.remove(store.object_with_guid(bpy.context.scene, root_guid), do_unlink=True)
        try:
            save.save_prefab(bpy.context.scene)
            check(False, "a multi-root scene is refused at save")
        except save.SaveError as error:
            check("root" in str(error), f"the refusal names the root rule: {str(error)[:80]}")
        try:
            instancing.add_instance(bpy.context.scene, prop_path, probe_layout)
            check(False, "placing an instance with no single root is refused")
        except instancing.InstanceError as error:
            check("root" in str(error), f"the instance refusal names the root rule: {error}")

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
