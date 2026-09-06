"""A group: a document object with nothing but meta and transform, shown as an EMPTY.

    blender --background --factory-startup --python tests/integration/test_groups.py

The format gets no group concept. A group is an ordinary object whose members are its children,
and Blender shows it as an Empty with ordinary parenting -- so a group can be placed, can hang
under any object, and moving it moves its members. What is pinned here: a nested document loads
into Blender parenting with local transforms intact, saves back byte-identical, and an Empty the
author makes and parents things to becomes a document object on save.
"""

from __future__ import annotations

import os
import sys
import tempfile

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import paradise_assets
from paradise_assets.document import prefab as prefab_document
from paradise_assets.document import project
from paradise_assets.materialize import grouping, load, save, store

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
INNER = "eeeeeeee-7777-4777-8777-777777777777"


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
        + obj_toml(GROUP, "Highway", ROOT, (10.0, 0.0, 0.0))    # a PLACED group
        + obj_toml(PLACED, "Deck", GROUP, (1.0, 2.0, 3.0))       # in the group
        + obj_toml(INNER, "Lamp", PLACED, (0.0, 0.0, 1.0))       # three deep
        + obj_toml(LOOSE, "Pivot", ROOT, (5.0, 0.0, 0.0))
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


def object_named(name: str):
    return next((o for o in bpy.context.scene.collection.all_objects if o.name == name), None)


def close(a, b) -> bool:
    return (Vector(a) - Vector(b)).length < 1e-5


def main() -> int:
    paradise_assets.register()
    try:
        with tempfile.TemporaryDirectory() as root:
            path = make_project(root)
            layout = project.locate(path)
            before = read(path)

            print("== a group loads as an Empty with its members parented to it ==")
            open_document(path, layout)
            highway = object_named("Highway")
            deck = object_named("Deck")
            lamp = object_named("Lamp")
            check(highway is not None and highway.type == "EMPTY", "'Highway' is an Empty")
            check(all(c.name != "Highway" for c in bpy.data.collections),
                  "and no collection was made for it")
            check(highway is not None and highway.parent is object_named("Level"),
                  "parented to the root")
            check(deck is not None and deck.parent is highway, "'Deck' is parented to the group")
            check(lamp is not None and lamp.parent is deck, "'Lamp' is parented three deep")

            print("\n== a placed group carries its transform and its members compose ==")
            bpy.context.view_layer.update()
            check(close(highway.location, (10.0, 0.0, 0.0)) if highway else False,
                  "the group keeps its own placement")
            # Document Y-up to Blender Z-up: (x, y, z) shows as (x, -z, y).
            check(close(deck.location, (1.0, -3.0, 2.0)) if deck else False,
                  "the member keeps its LOCAL placement")
            check(close(lamp.matrix_world.translation, (11.0, -4.0, 2.0)) if lamp else False,
                  f"and the grandchild sits at the composed world position "
                  f"({tuple(lamp.matrix_world.translation) if lamp else None})")

            print("\n== saving changes nothing ==")
            save.save_prefab(bpy.context.scene)
            check(read(path) == before, "the document is byte-identical after a load and a save")

            print("\n== moving the group moves its members, and only the group is rewritten ==")
            highway.location.x += 1.0
            save.save_prefab(bpy.context.scene)
            written = prefab_document.loads(read(path), path)
            moved = next(e for e in written.objects if e.name == "Highway")
            kept = next(e for e in written.objects if e.name == "Deck")
            check(list(moved.component(TRS).data["Position"])[0] == 11.0,
                  "the group's position moved")
            check(list(kept.component(TRS).data["Position"]) == [1.0, 2.0, 3.0],
                  "the member's local position is untouched")

            print("\n== an Empty the author makes and parents things to becomes a group ==")
            open_document(path, layout)
            pivot = object_named("Pivot")
            made = bpy.data.objects.new("Props", None)
            bpy.context.scene.collection.objects.link(made)
            made.location = (2.0, 0.0, 0.0)
            bpy.context.view_layer.update()
            world = pivot.matrix_world.copy()
            pivot.parent = made
            pivot.matrix_parent_inverse.identity()
            pivot.matrix_world = world

            save.save_prefab(bpy.context.scene)
            check(store.guid_of(made) is not None, "the Empty was given an identity")
            check(made.parent is object_named("Level"), "and hung off the document root")
            written = prefab_document.loads(read(path), path)
            group_entry = next((e for e in written.objects if e.name == "Props"), None)
            if check(group_entry is not None, "the document gained a 'Props' object"):
                check(group_entry.parent == ROOT, "whose parent is the root")
                check(
                    {c.id.lower() for c in group_entry.components} == {META.lower(), TRS.lower()},
                    "carrying meta and transform and nothing else",
                )
                check(list(group_entry.component(TRS).data["Position"])[0] == 2.0,
                      "with the placement the author gave it")
                child = next((e for e in written.objects if e.name == "Pivot"), None)
                check(child is not None and child.parent == group_entry.guid,
                      "and the object in it is now its child")
                check(child is not None
                      and list(child.component(TRS).data["Position"]) == [3.0, 0.0, 0.0],
                      "at a local position that keeps it where it was")

            print("\n== and comes back the same ==")
            round_tripped = read(path)
            open_document(path, layout)
            props = object_named("Props")
            check(props is not None and props.type == "EMPTY", "'Props' loads as an Empty")
            check(object_named("Pivot").parent is props, "still holding its object")
            save.save_prefab(bpy.context.scene)
            check(read(path) == round_tripped, "and a second save changes nothing again")

            print("\n== Group Selected: one gesture, nothing moves ==")
            open_document(path, layout)
            deck, pivot = object_named("Deck"), object_named("Pivot")
            bpy.context.view_layer.update()
            deck_world = deck.matrix_world.copy()
            pivot_world = pivot.matrix_world.copy()
            pivot_parent = pivot.parent
            group = grouping.group_objects(bpy.context.scene, [deck, pivot], active=pivot)
            bpy.context.view_layer.update()
            check(group.parent is pivot_parent, "the group hangs where the active member hung")
            check(close(group.matrix_world.translation, pivot_world.translation),
                  "and sits at the active member's position")
            check(deck.parent is group and pivot.parent is group, "both members are its children")
            check(close(deck.matrix_world.translation, deck_world.translation)
                  and close(pivot.matrix_world.translation, pivot_world.translation),
                  "and neither moved")
            check(object_named("Lamp").parent is deck, "a member's own child came along untouched")
            save.save_prefab(bpy.context.scene)
            written = prefab_document.loads(read(path), path)
            entry = next((e for e in written.objects if e.guid == store.guid_of(group)), None)
            check(entry is not None and entry.parent == store.guid_of(pivot_parent),
                  "the group is in the document under that parent")
            check(next(e for e in written.objects if e.name == "Deck").parent == store.guid_of(group),
                  "and the member's parent is the group")
            saved = read(path)
            open_document(path, layout)
            bpy.context.view_layer.update()
            check(close(object_named("Deck").matrix_world.translation, deck_world.translation),
                  "a reload puts the member back exactly where it was")
            save.save_prefab(bpy.context.scene)
            check(read(path) == saved, "and a second save changes nothing")

            print("\n== Ctrl+P style parenting, with a parent inverse, saves the right local ==")
            open_document(path, layout)
            deck, pivot = object_named("Deck"), object_named("Pivot")
            bpy.context.view_layer.update()
            deck_world = deck.matrix_world.copy()
            # What Ctrl+P (Object) does: bake the world into the basis, then store the parent's
            # inverse so the object stays put.
            deck.matrix_basis = deck_world
            deck.parent = pivot
            deck.matrix_parent_inverse = pivot.matrix_world.inverted()
            bpy.context.view_layer.update()
            check(close(deck.matrix_world.translation, deck_world.translation), "Blender keeps it in place")
            save.save_prefab(bpy.context.scene)
            open_document(path, layout)
            bpy.context.view_layer.update()
            check(close(object_named("Deck").matrix_world.translation, deck_world.translation),
                  f"and so does the document after a reload "
                  f"({tuple(object_named('Deck').matrix_world.translation)})")

            print("\n== what is not adopted ==")
            open_document(path, layout)
            stray = bpy.data.objects.new("Marker", None)
            bpy.context.scene.collection.objects.link(stray)
            save.save_prefab(bpy.context.scene)
            check(store.guid_of(stray) is None, "an Empty with nothing in it stays Blender's own")

            over = bpy.data.objects.new("Over", None)
            bpy.context.scene.collection.objects.link(over)
            object_named("Level").parent = over
            try:
                save.save_prefab(bpy.context.scene)
                check(False, "an Empty over the ROOT is refused, not adopted")
            except save.SaveError as error:
                check("Over" in str(error), "an Empty over the ROOT is refused, not adopted")
            check(store.guid_of(over) is None, "and was given no identity")
    finally:
        paradise_assets.unregister()

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
