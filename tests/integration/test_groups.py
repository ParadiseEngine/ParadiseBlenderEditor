"""A group: a document object with nothing but meta and transform, shown as a Blender COLLECTION.

    blender --background --factory-startup --python tests/integration/test_groups.py

The format gets no collection concept. A group is an ordinary object whose members are its
children, and only Blender shows it differently — so the risk is not corruption but DRIFT: load
and save disagreeing about which objects those are would move things between the scene and a
collection on every round trip and rewrite the document each time. The byte-exact save below is
what makes that visible.

The four rules that shape alone does not settle are pinned here too (see `groups.py`): never the
root, never an instance, identity transform only, at least one child.
"""

from __future__ import annotations

import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import paradise_assets
from paradise_assets.document import prefab as prefab_document
from paradise_assets.document import project
from paradise_assets.materialize import groups, load, save

failures: list[str] = []


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def check(condition: bool, label: str) -> bool:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)
    return condition


META = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
TRS = "7e55c210-3d41-4b8a-8f26-9c0a5e71b4d2"
ROOT = "aaaaaaaa-1111-4111-8111-111111111111"
GROUP = "bbbbbbbb-2222-4222-8222-222222222222"
PLACED = "cccccccc-3333-4333-8333-333333333333"
LOOSE = "dddddddd-4444-4444-8444-444444444444"


def obj_toml(guid: str, name: str, parent: str | None, position=None) -> str:
    text = (
        f'\n[[objects]]\n\n[[objects.components]]\nid = "{META}"\ntype = "meta"\n'
        f'Guid = "{guid}"\nName = "{name}"\n'
    )
    if parent:
        text += f'Parent = "{parent}"\n'
    x, y, z = position or (0.0, 0.0, 0.0)
    text += (
        f'\n[[objects.components]]\nid = "{TRS}"\ntype = "transform"\n'
        f"Position = [{x}, {y}, {z}]\nRotation = [0.0, 0.0, 0.0, 1.0]\nScale = [1.0, 1.0, 1.0]\n"
    )
    return text


def document_text() -> str:
    return (
        "schema_version = 1\n"
        + obj_toml(ROOT, "Level", None)
        + obj_toml(GROUP, "Highway", ROOT)                       # identity: a group
        + obj_toml(PLACED, "Deck", GROUP, (1.0, 2.0, 3.0))       # in the group
        + obj_toml(LOOSE, "Pivot", ROOT, (5.0, 0.0, 0.0))        # placed: stays an Empty
    )


def make_project(root: str) -> str:
    os.makedirs(os.path.join(root, "assets", "levels"))
    with open(os.path.join(root, "assets", "project.toml"), "w", encoding="utf-8") as handle:
        handle.write('schema_version = 1\nname = "grouptest"\n')
    path = os.path.join(root, "assets", "levels", "arena.prefab")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(document_text())
    with open(path + ".meta", "w", encoding="utf-8") as handle:
        handle.write('schema_version = 1\nguid = "eeeeeeee-5555-4555-8555-555555555555"\n')
    return path


def open_document(path: str, layout):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with open(path, encoding="utf-8") as handle:
        document = prefab_document.loads(handle.read(), path)
    return load.load_document(bpy.context.scene, document, path, layout)


def collection_named(name: str):
    return next((c for c in bpy.data.collections if c.name == name), None)


def object_named(name: str):
    return next((o for o in bpy.context.scene.collection.all_objects if o.name == name), None)


def main() -> int:
    paradise_assets.register()
    try:
        with tempfile.TemporaryDirectory() as root:
            path = make_project(root)
            layout = project.locate(path)
            before = read(path)

            print("== a group loads as a collection, not an object ==")
            open_document(path, layout)
            highway = collection_named("Highway")
            check(highway is not None, "'Highway' is a Blender collection")
            check(object_named("Highway") is None, "and NOT an object")
            check(
                highway is not None and groups.guid_of(highway) == GROUP,
                "it carries the document identity",
            )
            check(
                highway is not None and [o.name for o in highway.objects] == ["Deck"],
                f"its child is inside it ({[o.name for o in highway.objects] if highway else None})",
            )

            print("\n== the rules that shape alone does not settle ==")
            check(object_named("Level") is not None, "the ROOT stays an object")
            check(collection_named("Level") is None, "and is not a collection")
            check(
                object_named("Pivot") is not None and collection_named("Pivot") is None,
                "a PLACED empty stays an empty — a collection could not carry its transform",
            )

            print("\n== a group under an ordinary object stays an object ==")
            # Blender has no way to express it: `Collection` has no `parent` property at all and
            # `Collection.children` takes only Collections. Shown as a collection it would be
            # linked beside that object and saved back under the ROOT, silently reparenting
            # somebody's document. So it stays an Empty and the document survives untouched.
            nested = path.replace("arena.prefab", "nested.prefab")
            with open(nested, "w", encoding="utf-8") as handle:
                handle.write(
                    "schema_version = 1\n"
                    + obj_toml(ROOT, "Level", None)
                    + obj_toml(LOOSE, "Placed", ROOT, (7.0, 0.0, 0.0))
                    + obj_toml(GROUP, "Under", LOOSE)
                    + obj_toml(PLACED, "Member", GROUP, (1.0, 0.0, 0.0))
                )
            with open(nested + ".meta", "w", encoding="utf-8") as handle:
                handle.write('schema_version = 1\nguid = "ffffffff-6666-4666-8666-666666666666"\n')
            was = read(nested)
            open_document(nested, layout)
            check(collection_named("Under") is None, "'Under' is NOT a collection")
            check(object_named("Under") is not None, "it stays an object")
            save.save_prefab(bpy.context.scene)
            check(read(nested) == was, "and saving does not reparent it under the root")
            open_document(path, layout)

            print("\n== saving changes nothing ==")
            save.save_prefab(bpy.context.scene)
            check(
                read(path) == before,
                "the document is byte-identical after a load and a save",
            )

            print("\n== a collection an author makes becomes a group object ==")
            made = bpy.data.collections.new("Props")
            bpy.context.scene.collection.children.link(made)
            pivot = object_named("Pivot")
            for holder in list(pivot.users_collection):
                holder.objects.unlink(pivot)
            made.objects.link(pivot)
            # NOT unparented. Dragging a row into a collection in the Outliner does not clear
            # its object parenting, and every object in a real level is parented to the root --
            # so a rule that let parenting win here made groups impossible to author (#41).

            save.save_prefab(bpy.context.scene)
            written = prefab_document.loads(read(path), path)
            group_entry = next((e for e in written.objects if e.name == "Props"), None)
            if check(group_entry is not None, "the document gained a 'Props' object"):
                check(
                    {c.id.lower() for c in group_entry.components} == {META.lower(), TRS.lower()},
                    "carrying meta and transform and nothing else",
                )
                trs = group_entry.component(TRS)
                check(
                    list(trs.data["Position"]) == [0.0, 0.0, 0.0]
                    and list(trs.data["Scale"]) == [1.0, 1.0, 1.0],
                    "with an identity transform",
                )
                moved = next((e for e in written.objects if e.name == "Pivot"), None)
                check(
                    moved is not None and moved.parent == group_entry.guid,
                    "and the object in it is now its child",
                )

            print("\n== and comes back as a collection ==")
            open_document(path, layout)
            props = collection_named("Props")
            check(props is not None, "'Props' loads as a collection")
            check(
                props is not None and [o.name for o in props.objects] == ["Pivot"],
                f"still holding its object ({[o.name for o in props.objects] if props else None})",
            )
            round_tripped = read(path)
            save.save_prefab(bpy.context.scene)
            check(
                read(path) == round_tripped,
                "and a second save changes nothing again",
            )
    finally:
        paradise_assets.unregister()

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
