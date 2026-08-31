"""Blender's save writes the prefab document too.

    blender --background --factory-startup --python tests/integration/test_save_on_save.py

Its own throwaway project, because every check here WRITES -- pointing this at a real one would
edit the tree it was meant to be testing against.

Two of these matter more than the rest.

**Opening a document must not modify it.** `ops.open_prefab` saves the working file as part of
opening, and `workfile.save` goes through `save_as_mainfile`, which fires the same `save_pre` the
feature hangs on. Unsuppressed, merely opening a level would rewrite it -- dirty in `git status`,
mtime bumped for nothing, and that prefab's rendered thumbnail invalidated. Nothing about that
looks broken at the time.

**A stale document must be refused and SAID.** A handler can neither open a dialog nor cancel the
save, so a refusal that is not recorded is a save the author believes happened.
"""

from __future__ import annotations

import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import paradise_assets  # noqa: E402
from paradise_assets.document import project  # noqa: E402
from paradise_assets.document.prefab import loads as parse_document  # noqa: E402
from paradise_assets.materialize import load, store, sync, workfile  # noqa: E402

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


def make_project(root: str) -> str:
    levels = os.path.join(root, "assets", "levels")
    os.makedirs(levels)
    with open(os.path.join(root, "assets", "project.toml"), "w", encoding="utf-8") as handle:
        handle.write('schema_version = 1\nname = "synctest"\n')
    document = os.path.join(levels, "arena.prefab")
    with open(document, "w", encoding="utf-8", newline="") as handle:
        handle.write(PREFAB)
    with open(document + ".meta", "w", encoding="utf-8", newline="") as handle:
        handle.write(META)
    return document


def fingerprint(path: str) -> tuple:
    info = os.stat(path)
    return (info.st_mtime_ns, info.st_size)


def crate_position(document_path: str):
    with open(document_path, encoding="utf-8") as handle:
        document = parse_document(handle.read(), document_path)
    for entry in document.objects:
        if entry.name == "Crate":
            component = entry.component("7e55c210-3d41-4b8a-8f26-9c0a5e71b4d2")
            return None if component is None else component.data.get("Position")
    return None


def materialize(document_path: str, layout):
    with open(document_path, encoding="utf-8") as handle:
        document = parse_document(handle.read(), document_path)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    load.load_document(bpy.context.scene, document, document_path, layout)
    return bpy.context.scene


def main() -> int:
    paradise_assets.register()
    try:
        with tempfile.TemporaryDirectory() as work:
            root = os.path.join(work, "game")
            os.makedirs(root)
            document = make_project(root)
            layout = project.locate(document)

            print("== opening a document does not modify it ==")
            before = fingerprint(document)
            scene = materialize(document, layout)
            written = workfile.save(layout, document)
            check(written is not None, "the working file was written")
            check(
                fingerprint(document) == before,
                "opening left the document byte-for-byte untouched",
            )

            print("\n== Ctrl+S reaches the document ==")
            crate = next(
                o for o in bpy.context.scene.collection.all_objects
                if store.guid_of(o) == "bbbbbbbb-1111-4222-8333-444444444444"
            )
            crate.location.x += 5.0
            bpy.ops.wm.save_mainfile()

            moved = crate_position(document)
            check(moved is not None, f"the crate still has a transform ({moved})")
            check(
                moved is not None and abs(moved[0] - 6.0) < 1e-4,
                f"the move is in the document ({moved[0] if moved else None} — expected 6.0)",
            )
            check(sync.refusal(bpy.context.scene) is None, "and nothing was refused")

            print("\n== a save that changes nothing rewrites nothing ==")
            steady = open(document, "rb").read()
            bpy.ops.wm.save_mainfile()
            check(
                open(document, "rb").read() == steady,
                "an unchanged save leaves the document byte-identical",
            )

            print("\n== a stale document is refused, and says so ==")
            # Behind the addon's back, as another tool or a `git pull` would.
            with open(document, "a", encoding="utf-8", newline="") as handle:
                handle.write("\n# touched externally\n")
            external = open(document, "rb").read()

            crate.location.x += 5.0
            bpy.ops.wm.save_mainfile()

            check(
                open(document, "rb").read() == external,
                "the external change survived — the save did not clobber it",
            )
            refusal = sync.refusal(bpy.context.scene)
            check(refusal is not None, f"the refusal is recorded for the panel ({refusal})")
            check(
                refusal is not None and "changed on disk" in refusal,
                "and it says why",
            )
            # The .blend still saved: the edit is in the working file, which is the whole reason
            # a refusal is safe rather than a loss.
            check(os.path.isfile(written), "the working file was still written")

            print("\n== a reload clears the refusal ==")
            scene = materialize(document, layout)
            workfile.save(layout, document)
            bpy.ops.wm.save_mainfile()
            check(
                sync.refusal(bpy.context.scene) is None,
                "a successful save clears the previous refusal",
            )

            print("\n== a .blend that is not a working file writes nothing ==")
            other = os.path.join(work, "unrelated.blend")
            bpy.ops.wm.read_factory_settings(use_empty=True)
            check(
                store.read_state(bpy.context.scene) is None,
                "the scene carries no document state",
            )
            steady = open(document, "rb").read()
            bpy.ops.wm.save_as_mainfile(filepath=other)
            check(
                open(document, "rb").read() == steady,
                "saving an unrelated .blend left the document alone",
            )
    finally:
        paradise_assets.unregister()

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
