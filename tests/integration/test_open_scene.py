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
import os
import shutil
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from paradise_assets.document import project, prefab as prefab_document, well_known  # noqa: E402
from paradise_assets.materialize import load, save, store  # noqa: E402

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
    # folders. The extension is spelled out here rather than taken from `project.SCENE_SUFFIX` --
    # that constant still says ".scene" and nothing else reads it, so trusting it would silently
    # find zero documents and pass every check below by vacuum. Same glob `catalogue.build` uses.
    documents = sorted(glob.glob(os.path.join(layout.assets, "**", "*.prefab"), recursive=True))
    check(bool(documents), f"found {len(documents)} document(s) under {layout.assets}")
    if not documents:
        # Everything past here indexes documents[0]; without this the suite dies with an
        # IndexError that says nothing about the actual problem.
        print("\n(no documents to exercise -- stopping here)")
        return 1

    print("\n== the round trip: open -> save must be byte-identical ==")
    for path in documents:
        original = open(path, "rb").read()
        with tempfile.TemporaryDirectory() as work:
            copy = os.path.join(work, os.path.basename(path))
            shutil.copy2(path, copy)

            result = open_document(copy, layout)
            saved = save.save_prefab(bpy.context.scene)

            check(
                open(copy, "rb").read() == original,
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
        before = prefab_document.loads(open(copy, encoding="utf-8").read(), copy)

        open_document(copy, layout)
        moved = next(o for o in bpy.context.scene.collection.all_objects if store.guid_of(o))
        target = store.guid_of(moved)
        moved.location.x += 5.0
        result = save.save_prefab(bpy.context.scene)

        after = prefab_document.loads(open(copy, encoding="utf-8").read(), copy)
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
                'id = "99999999-8888-4777-8666-555555555555"\n'
                'type = "Nobody.Has.Heard.Of.This"\n'
                "Weird = 42\n"
                'Nested = "keep me"\n'
            )
        original = open(copy, "rb").read()
        open_document(copy, layout)
        save.save_prefab(bpy.context.scene)
        check(open(copy, "rb").read() == original, "an unknown component is written back verbatim")

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
                "\n[[objects]]\n\n[[objects.components]]\n"
                f'id = "{well_known.META_ID}"\ntype = "meta"\nGuid = "{child_local}"\n'
                f'Name = "Bulb"\nParent = "{root_local}"\n'
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
            )

        original = open(scene_path, "rb").read()

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
        check(open(scene_path, "rb").read() == original, "the instance is written back unflattened")

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
