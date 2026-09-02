"""Opening a cached ``.editor/blend/*.blend`` refreshes from assets and starts the watcher.

    blender --background --factory-startup --python tests/integration/test_open_workfile.py

The working file is a CACHE. Opening it directly (File > Open, a recent file, double-click)
used to show whatever objects were last saved into it, even if a prefab, a nested asset or a
git pull had moved on. ``load_post`` rematerializes from the document and starts
``paradise assets watch`` for that project -- the same postcondition as Open Prefab.

Its own throwaway project: every check here writes, and a real one would be the thing under test.
"""

from __future__ import annotations

import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import paradise_assets
from paradise_assets import watch
from paradise_assets.document import project
from paradise_assets.document.prefab import loads as parse_document
from paradise_assets.materialize import load, store, workfile

failures: list[str] = []


def check(condition: bool, label: str) -> bool:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)
    return condition


PREFAB = """schema_version = 1

[[objects]]

[[objects.components]]
id = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
type = "meta"
Guid = "aaaaaaaa-1111-4222-8333-444444444444"
Name = "Root"

[[objects]]

[[objects.components]]
id = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
type = "meta"
Guid = "bbbbbbbb-1111-4222-8333-444444444444"
Name = "Crate"
Parent = "aaaaaaaa-1111-4222-8333-444444444444"

[[objects.components]]
id = "7e55c210-3d41-4b8a-8f26-9c0a5e71b4d2"
type = "transform"
Position = [1.0, 2.0, 3.0]
Rotation = [0.0, 0.0, 0.0, 1.0]
Scale = [1.0, 1.0, 1.0]
"""

META = 'schema_version = 1\nguid = "55555555-6666-4777-8888-999999999999"\nkind = "document"\n'

EXTRA_OBJECT = """
[[objects]]

[[objects.components]]
id = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
type = "meta"
Guid = "cccccccc-1111-4222-8333-444444444444"
Name = "AddedOutside"
Parent = "aaaaaaaa-1111-4222-8333-444444444444"
"""


def make_project(root: str) -> str:
    levels = os.path.join(root, "assets", "levels")
    os.makedirs(levels)
    with open(os.path.join(root, "assets", "project.toml"), "w", encoding="utf-8") as handle:
        handle.write('schema_version = 1\nname = "workfiletest"\n')
    document = os.path.join(levels, "arena.prefab")
    with open(document, "w", encoding="utf-8", newline="") as handle:
        handle.write(PREFAB)
    with open(document + ".meta", "w", encoding="utf-8", newline="") as handle:
        handle.write(META)
    return document


def materialize(document_path: str, layout):
    with open(document_path, encoding="utf-8") as handle:
        document = parse_document(handle.read(), document_path)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    load.load_document(bpy.context.scene, document, document_path, layout)
    return bpy.context.scene


def names_in_scene() -> set[str]:
    return {obj.name for obj in bpy.context.scene.collection.all_objects if store.guid_of(obj)}


def main() -> int:
    paradise_assets.register()
    started: list[str] = []
    original_start_for = watch.start_for
    watch.start_for = lambda root: started.append(os.path.normcase(root)) or None
    try:
        with tempfile.TemporaryDirectory() as work:
            root = os.path.join(work, "game")
            os.makedirs(root)
            document = make_project(root)
            layout = project.locate(document)

            print("== a workfile keeps extras and is rewritten from the document on open ==")
            materialize(document, layout)
            extra = bpy.data.objects.new("AuthorNote", None)
            bpy.context.scene.collection.objects.link(extra)
            written = workfile.save(layout, document)
            check(written is not None, "the working file was written")
            check(
                names_in_scene() == {"Root", "Crate"},
                f"the cache first held Root and Crate ({names_in_scene()})",
            )

            # Behind Blender's back: another tool, a git pull, a nested prefab edit.
            with open(document, "a", encoding="utf-8", newline="") as handle:
                handle.write(EXTRA_OBJECT)

            started.clear()
            bpy.ops.wm.open_mainfile(filepath=written)

            check(
                names_in_scene() == {"Root", "Crate", "AddedOutside"},
                f"opening the .blend rematerialized the external edit ({names_in_scene()})",
            )
            check(
                "AuthorNote" in {
                    obj.name for obj in bpy.context.scene.collection.all_objects
                    if store.guid_of(obj) is None
                },
                "and kept the extra that is not a document object",
            )
            check(
                started == [os.path.normcase(layout.root)],
                f"and started the watcher for the project ({started})",
            )

            print("\n== Open Prefab into an existing workfile starts the watcher too ==")
            started.clear()
            bpy.ops.paradise_assets.open_prefab(filepath=document)
            check(
                os.path.normcase(layout.root) in started,
                f"opening the prefab started the watcher ({started})",
            )
            check(
                names_in_scene() == {"Root", "Crate", "AddedOutside"},
                f"and still shows the document ({names_in_scene()})",
            )
    finally:
        watch.start_for = original_start_for
        paradise_assets.unregister()

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
