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

import os
import shutil
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from paradise_assets.document import project, scene as scene_document  # noqa: E402
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
        document = scene_document.loads(handle.read(), path)
    return load.load_document(fresh_scene(), document, path, layout)


def main() -> int:
    root = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else DEFAULT_PROJECT
    layout = project.locate(root)
    if layout is None:
        # Not a failure: this addon's tests need a real asset project, and a clone of this
        # repository alone has none. Skipping loudly beats a red suite nobody can fix here.
        print(f"SKIP: no asset project at or above {root}")
        return 0

    scenes = sorted(
        os.path.join(layout.scenes, name)
        for name in os.listdir(layout.scenes)
        if name.endswith(project.SCENE_SUFFIX)
    )
    check(bool(scenes), f"found {len(scenes)} scene document(s) under {layout.scenes}")

    print("\n== the round trip: open -> save must be byte-identical ==")
    for path in scenes:
        original = open(path, "rb").read()
        with tempfile.TemporaryDirectory() as work:
            copy = os.path.join(work, os.path.basename(path))
            shutil.copy2(path, copy)

            result = open_document(copy, layout)
            saved = save.save_scene(bpy.context.scene)

            check(
                open(copy, "rb").read() == original,
                f"{os.path.basename(path)}: {result.objects} objects round-trip byte for byte",
            )
            check(saved.moved == 0, f"{os.path.basename(path)}: nothing reported as moved")

    print("\n== the document is materialized faithfully ==")
    test_scene = os.path.join(layout.scenes, "test" + project.SCENE_SUFFIX)
    if os.path.isfile(test_scene):
        with open(test_scene, encoding="utf-8") as handle:
            document = scene_document.loads(handle.read(), test_scene)
        result = open_document(test_scene, layout)

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
        copy = os.path.join(work, "edit.scene")
        shutil.copy2(scenes[0], copy)
        before = scene_document.loads(open(copy, encoding="utf-8").read(), copy)

        open_document(copy, layout)
        moved = next(o for o in bpy.context.scene.collection.all_objects if store.guid_of(o))
        target = store.guid_of(moved)
        moved.location.x += 5.0
        result = save.save_scene(bpy.context.scene)

        after = scene_document.loads(open(copy, encoding="utf-8").read(), copy)
        check(result.moved == 1, "exactly one object reported as moved")

        # Compared as DOCUMENTS, not as lines: giving an object its first transform inserts a
        # table, which shifts every line below it -- a textual diff would call the whole file
        # changed and say nothing about whether anything really did.
        old, new = before.by_guid(), after.by_guid()
        check(set(old) == set(new), "no object appeared or disappeared")
        differing = [g for g in old if old[g].transform != new[g].transform]
        check(differing == [target], f"exactly the moved object's transform changed ({len(differing)})")
        check(
            all(old[g].components == new[g].components for g in old),
            "no component payload changed",
        )
        check(
            all((old[g].name, old[g].parent) == (new[g].name, new[g].parent) for g in old),
            "no name or parent changed",
        )

    print("\n== the stamp refuses a save over an external change ==")
    with tempfile.TemporaryDirectory() as work:
        copy = os.path.join(work, "stale.scene")
        shutil.copy2(scenes[0], copy)
        open_document(copy, layout)

        # Rewrite it behind the addon's back, as another tool or a `git pull` would.
        with open(copy, "a", encoding="utf-8") as handle:
            handle.write("\n")

        try:
            save.save_scene(bpy.context.scene)
            check(False, "a stale document refuses the save")
        except save.SaveError as error:
            check("changed on disk" in str(error), "a stale document refuses the save")

    print("\n== an unrecognised component survives the round trip ==")
    with tempfile.TemporaryDirectory() as work:
        copy = os.path.join(work, "unknown.scene")
        with open(copy, "w", encoding="utf-8", newline="") as handle:
            handle.write(
                "schema_version = 1\n"
                "\n[[objects]]\n"
                'guid = "11111111-2222-4333-8444-555555555555"\n'
                'name = "thing"\n'
                "\n[[objects.components]]\n"
                'id = "99999999-8888-4777-8666-555555555555"\n'
                'type = "Nobody.Has.Heard.Of.This"\n'
                "\n[objects.components.data]\n"
                "Weird = 42\n"
                'Nested = "keep me"\n'
            )
        original = open(copy, "rb").read()
        open_document(copy, layout)
        save.save_scene(bpy.context.scene)
        check(open(copy, "rb").read() == original, "an unknown component is written back verbatim")

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
