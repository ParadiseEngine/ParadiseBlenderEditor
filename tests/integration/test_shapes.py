"""Collision shapes: a host-authored ``Shapes`` list shown as Empties under the object.

    blender --background --factory-startup --python tests/integration/test_shapes.py

The schema says ``authoredBy: shape``, so the panel cannot type the geometry; the Empty is the
editor. Pinned: load makes one Empty per row with the document's placement and extents, an
untouched scene saves byte-identical, moving an Empty rewrites that row's geometry and nothing
else, deleting one drops the row, adding one appends a complete row, and a reload rebuilds them.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import paradise_assets
from paradise_assets.document import prefab as prefab_document
from paradise_assets.document import project
from paradise_assets.materialize import load, save, shapes

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
COLLIDER = "a5cac2a4-bd53-4598-9a10-28d2039e6b99"
MARKER = "e164ec51-2fa5-4faa-aaa7-f36d1f34c620"
ROOT = "aaaaaaaa-1111-4111-8111-111111111111"
CAR = "cccccccc-3333-4333-8333-333333333333"

SHAPE_FIELDS = [
    {"name": "ShapeType", "type": "enum", "values": ["Box", "Sphere", "Capsule"]},
    {"name": "LocalCenter", "type": "vector3"},
    {"name": "LocalRotation", "type": "quaternion"},
    {"name": "Size", "type": "vector3"},
    {"name": "Radius", "type": "float"},
    {"name": "Height", "type": "float"},
]

SCHEMA = {"components": [{
    "id": MARKER, "type": "Game.CameraTriggerMarker", "displayName": "Camera Trigger",
    "fields": [
        {"name": "Yaw", "type": "float"},
        {"name": "Volume", "type": "object", "fields": [
            {"name": "Shape", "type": "object", "authoredBy": "shape", "fields": SHAPE_FIELDS},
            {"name": "IsTrigger", "type": "bool"},
        ]},
    ],
}, {
    "id": COLLIDER, "type": "Game.AuthoredColliders", "displayName": "Colliders",
    "fields": [{"name": "Shapes", "type": "array", "items": {
        "name": "Shapes", "type": "object", "fields": [
            {"name": "Shape", "type": "object", "authoredBy": "shape", "fields": [
                {"name": "ShapeType", "type": "enum", "values": ["Box", "Sphere", "Capsule"]},
                {"name": "LocalCenter", "type": "vector3"},
                {"name": "LocalRotation", "type": "quaternion"},
                {"name": "Size", "type": "vector3"},
                {"name": "Radius", "type": "float"},
                {"name": "Height", "type": "float"},
            ]},
            {"name": "IsTrigger", "type": "bool"},
            {"name": "Layer", "type": "int"},
        ]}}],
}]}

DOCUMENT = f'''schema_version = 1

[[objects]]

[[objects.components]]
id = "{META}"
type = "meta"
Guid = "{ROOT}"
Name = "Level"

[[objects.components]]
id = "{TRS}"
type = "transform"
Position = [0.0, 0.0, 0.0]
Rotation = [0.0, 0.0, 0.0, 1.0]
Scale = [1.0, 1.0, 1.0]

[[objects]]

[[objects.components]]
id = "{META}"
type = "meta"
Guid = "{CAR}"
Name = "Car"
Parent = "{ROOT}"

[[objects.components]]
id = "{TRS}"
type = "transform"
Position = [0.0, 0.55, 38.0]
Rotation = [0.0, 0.0, 0.0, 1.0]
Scale = [1.0, 1.0, 1.0]

[[objects.components]]
id = "{COLLIDER}"
type = "Game.AuthoredColliders"

[[objects.components.Shapes]]
IsTrigger = false
Layer = 0

[objects.components.Shapes.Shape]
ShapeType = "Box"
LocalCenter = [0, 0.25, 0]
LocalRotation = [0, 0, 0, 1]
Size = [2, 1.6, 4]
Radius = 0.0
Height = 0.0

[[objects.components.Shapes]]
IsTrigger = true
Layer = 2

[objects.components.Shapes.Shape]
ShapeType = "Sphere"
LocalCenter = [0, 0, 2]
LocalRotation = [0, 0, 0, 1]
Size = [0, 0, 0]
Radius = 0.4
Height = 0.0
'''


def make_project(root: str) -> str:
    os.makedirs(os.path.join(root, "assets", "levels"))
    os.makedirs(os.path.join(root, ".editor"))
    with open(os.path.join(root, "assets", "project.toml"), "w", encoding="utf-8") as handle:
        handle.write('schema_version = 1\nname = "shapetest"\n')
    with open(os.path.join(root, ".editor", "authoring-schema.json"), "w", encoding="utf-8") as handle:
        json.dump(SCHEMA, handle)
    path = os.path.join(root, "assets", "levels", "arena.prefab")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(DOCUMENT)
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


def shapes_of(path: str) -> list:
    document = prefab_document.loads(read(path), path)
    car = next(e for e in document.objects if e.name == "Car")
    return car.component(COLLIDER).data["Shapes"]


def geometry(row: dict) -> dict:
    return row["Shape"]


def close(a, b, eps=1e-5) -> bool:
    return all(abs(float(x) - float(y)) <= eps for x, y in zip(a, b, strict=True))


def main() -> int:
    paradise_assets.register()
    try:
        with tempfile.TemporaryDirectory() as root:
            path = make_project(root)
            layout = project.locate(path)
            before = read(path)

            print("== each shape row is an Empty under its object ==")
            open_document(path, layout)
            car = object_named("Car")
            empties = shapes.shape_empties(car, COLLIDER, "Shapes")
            check(len(empties) == 2, f"two shape Empties ({[e.name for e in empties]})")
            body, bumper = [*empties, None, None][:2]
            check(body is not None and body.parent is car and shapes.is_shape(body),
                  "parented to the object and tagged as a shape")
            check(body is not None and body.empty_display_type == "CUBE"
                  and close(body.scale, (2, 4, 1.6)), "a box's scale is its Size, rebased to Z-up")
            check(body is not None and close(body.location, (0, 0, 0.25)),
                  "placed at its LocalCenter, rebased")
            check(bumper is not None and bumper.empty_display_type == "SPHERE"
                  and abs(bumper.empty_display_size - 0.4) < 1e-6, "a sphere's display size is its Radius")
            check(all(shapes.is_shape(o) is False or o.get("paradise_guid") is None
                      for o in bpy.context.scene.collection.all_objects),
                  "shape Empties carry no document identity")

            print("\n== an untouched scene saves byte-identical ==")
            save.save_prefab(bpy.context.scene)
            check(read(path) == before, "nothing rewritten")

            print("\n== moving an Empty rewrites that row's geometry, and only that ==")
            body.location.x += 1.0
            body.scale.z = 2.0
            save.save_prefab(bpy.context.scene)
            rows = shapes_of(path)
            check(close(geometry(rows[0])["LocalCenter"], (1, 0.25, 0)),
                  f"LocalCenter moved ({geometry(rows[0])['LocalCenter']})")
            check(close(geometry(rows[0])["Size"], (2, 2, 4)),
                  f"Size follows the scale ({geometry(rows[0])['Size']})")
            check(rows[0]["IsTrigger"] is False and rows[0]["Layer"] == 0,
                  "the row's game members are untouched")
            check(geometry(rows[1])["Radius"] == 0.4 and rows[1]["IsTrigger"] is True,
                  "the other row is untouched")
            again = read(path)
            save.save_prefab(bpy.context.scene)
            check(read(path) == again, "and a second save changes nothing")

            print("\n== deleting an Empty drops its row ==")
            bpy.data.objects.remove(body, do_unlink=True)
            save.save_prefab(bpy.context.scene)
            rows = shapes_of(path)
            check(len(rows) == 1 and rows[0]["IsTrigger"] is True,
                  f"one row left ({[geometry(r).get('ShapeType') for r in rows]})")

            print("\n== the panel plans from the Empties, not the last save ==")
            from paradise_assets.document import component_schema
            vocabulary = component_schema.load(layout.root)
            schema = vocabulary.get(COLLIDER)
            def planned_rows():
                data = {"Shapes": [dict(r) for r in shapes_of(path)]}
                live = shapes.overlay_live(car, COLLIDER, schema, data, shapes.default_row)
                return [i for i in schema.plan(live) if i.role == component_schema.ROLE_ROW]
            check(len(planned_rows()) == 1, "one row after the delete, before any save")
            extra = shapes.add_shape(car, COLLIDER, "Shapes", "Sphere")
            check(len(planned_rows()) == 2, "two rows the moment an Empty is added")
            bpy.data.objects.remove(extra, do_unlink=True)
            for e in shapes.shape_empties(car, COLLIDER, "Shapes"):
                bpy.data.objects.remove(e, do_unlink=True)
            check(len(planned_rows()) == 0, "and none once every Empty is gone")
            open_document(path, layout)
            car = object_named("Car")

            print("\n== adding a shape appends a complete row ==")
            capsule = shapes.add_shape(car, COLLIDER, "Shapes", "Capsule")
            capsule.location = (0, 0, 1.0)          # Blender Z is document Y
            save.save_prefab(bpy.context.scene)
            rows = shapes_of(path)
            check(len(rows) == 2 and geometry(rows[1])["ShapeType"] == "Capsule",
                  "a Capsule row was appended")
            check(geometry(rows[1])["Radius"] == 0.25 and geometry(rows[1])["Height"] == 1.0,
                  "with the default extents")
            check(close(geometry(rows[1])["LocalCenter"], (0, 1, 0)),
                  f"where the Empty was put ({geometry(rows[1])['LocalCenter']})")
            check(rows[1].get("IsTrigger") is False and rows[1].get("Layer") == 0,
                  "and every game member at its schema default")

            print("\n== an instance's own collider is shown; its prefab's is not ==")
            os.makedirs(os.path.join(root, "assets", "props"))
            prop = os.path.join(root, "assets", "props", "crate.prefab")
            PROP = "11111111-2222-4333-8444-555555555555"
            with open(prop, "w", encoding="utf-8") as handle:
                handle.write(
                    f'schema_version = 1\n\n[[objects]]\n\n[[objects.components]]\nid = "{META}"\n'
                    f'type = "meta"\nGuid = "{PROP}"\nName = "Crate"\n\n[[objects.components]]\n'
                    f'id = "{COLLIDER}"\ntype = "Game.AuthoredColliders"\n\n[[objects.components.Shapes]]\n'
                    'IsTrigger = false\n\n[objects.components.Shapes.Shape]\n'
                    'ShapeType = "Box"\nSize = [1, 1, 1]\n')
            with open(prop + ".meta", "w", encoding="utf-8") as handle:
                handle.write(f'schema_version = 1\nguid = "{PROP}"\n')
            level = path.replace("arena.prefab", "placed.prefab")
            OWN = "dddddddd-4444-4444-8444-444444444444"
            FROM_PREFAB = "dddddddd-5555-4555-8555-555555555555"
            with open(level, "w", encoding="utf-8") as handle:
                handle.write(
                    DOCUMENT.split("[[objects]]", 2)[0]
                    + "[[objects]]" + DOCUMENT.split("[[objects]]", 2)[1]
                    + f'[[objects]]\nprefab = {{ guid = "{PROP}", path = "props/crate.prefab" }}\n\n'
                    f'[[objects.components]]\nid = "{META}"\ntype = "meta"\nGuid = "{OWN}"\n'
                    f'Name = "OwnCollider"\nParent = "{ROOT}"\n\n[[objects.components]]\n'
                    f'id = "{COLLIDER}"\ntype = "Game.AuthoredColliders"\n\n[[objects.components.Shapes]]\n'
                    'IsTrigger = true\n\n[objects.components.Shapes.Shape]\n'
                    'ShapeType = "Sphere"\nRadius = 2.0\n\n'
                    f'[[objects]]\nprefab = {{ guid = "{PROP}", path = "props/crate.prefab" }}\n\n'
                    f'[[objects.components]]\nid = "{META}"\ntype = "meta"\nGuid = "{FROM_PREFAB}"\n'
                    f'Name = "PrefabCollider"\nParent = "{ROOT}"\n')
            with open(level + ".meta", "w", encoding="utf-8") as handle:
                handle.write('schema_version = 1\nguid = "eeeeeeee-6666-4666-8666-666666666666"\n')
            open_document(level, layout)
            own = shapes.shape_empties(object_named("OwnCollider"), COLLIDER, "Shapes")
            check(len(own) == 1 and own[0].empty_display_type == "SPHERE",
                  f"an instance that authors its collider gets its Empty ({len(own)})")
            check(not shapes.shape_empties(object_named("PrefabCollider"), COLLIDER, "Shapes"),
                  "one whose collider comes from the prefab gets none")
            own[0].location.x = 3.0
            save.save_prefab(bpy.context.scene)
            placed = prefab_document.loads(read(level), level)
            moved = next(e for e in placed.objects if e.name == "OwnCollider")
            check(close(moved.component(COLLIDER).data["Shapes"][0]["Shape"]["LocalCenter"], (3, 0, 0)),
                  "and moving it writes the instance's own row")
            untouched = next(e for e in placed.objects if e.name == "PrefabCollider")
            check(untouched.component(COLLIDER) is None, "without inventing a collider on the other")
            open_document(path, layout)

            print("\n== a marker's Volume is ONE shape: its own Empty, absent when deleted ==")
            marked = path.replace("arena.prefab", "marked.prefab")
            GUIDE = "dddddddd-9999-4999-8999-999999999999"
            with open(marked, "w", encoding="utf-8") as handle:
                handle.write(
                    DOCUMENT.split("[[objects]]", 2)[0] + "[[objects]]" + DOCUMENT.split("[[objects]]", 2)[1]
                    + f'[[objects]]\n\n[[objects.components]]\nid = "{META}"\ntype = "meta"\n'
                    f'Guid = "{GUIDE}"\nName = "Guide"\nParent = "{ROOT}"\n\n'
                    f'[[objects.components]]\nid = "{MARKER}"\ntype = "Game.CameraTriggerMarker"\n'
                    'Yaw = 40.0\n\n[objects.components.Volume]\nIsTrigger = true\n\n'
                    '[objects.components.Volume.Shape]\nShapeType = "Sphere"\nLocalCenter = [0, 0, 0]\n'
                    'LocalRotation = [0, 0, 0, 1]\nSize = [0, 0, 0]\nRadius = 8.0\nHeight = 0.0\n')
            with open(marked + ".meta", "w", encoding="utf-8") as handle:
                handle.write('schema_version = 1\nguid = "eeeeeeee-8888-4888-8888-888888888888"\n')
            untouched = read(marked)
            open_document(marked, layout)
            guide = object_named("Guide")
            volume = shapes.shape_empties(guide, MARKER, "Volume")
            check(len(volume) == 1 and volume[0].empty_display_type == "SPHERE"
                  and abs(volume[0].empty_display_size - 8.0) < 1e-6,
                  "the Volume is a sphere Empty of the authored radius")
            save.save_prefab(bpy.context.scene)
            check(read(marked) == untouched, "an untouched Volume saves byte-identical")
            volume[0].empty_display_size = 5.0
            save.save_prefab(bpy.context.scene)
            written = prefab_document.loads(read(marked), marked)
            marker = next(e for e in written.objects if e.name == "Guide").component(MARKER)
            check(marker.data["Volume"]["Shape"]["Radius"] == 5.0
                  and marker.data["Volume"]["IsTrigger"] is True,
                  f"resizing it writes Volume.Shape and keeps IsTrigger ({marker.data['Volume']})")
            try:
                shapes.add_shape(guide, MARKER, "Volume", "Box", single=True)
                check(False, "a second Volume is refused")
            except ValueError:
                check(True, "a second Volume is refused")
            bpy.data.objects.remove(volume[0], do_unlink=True)
            save.save_prefab(bpy.context.scene)
            written = prefab_document.loads(read(marked), marked)
            marker = next(e for e in written.objects if e.name == "Guide").component(MARKER)
            check("Volume" not in marker.data and marker.data["Yaw"] == 40.0,
                  f"deleting the Empty removes Volume and nothing else ({marker.data})")
            made = shapes.add_shape(guide, MARKER, "Volume", "Box", single=True)
            made.scale = (2.0, 2.0, 2.0)
            save.save_prefab(bpy.context.scene)
            written = prefab_document.loads(read(marked), marked)
            marker = next(e for e in written.objects if e.name == "Guide").component(MARKER)
            check(marker.data.get("Volume", {}).get("Shape", {}).get("ShapeType") == "Box"
                  and marker.data["Volume"]["IsTrigger"] is False,
                  f"adding one back writes a complete row ({marker.data.get('Volume')})")
            open_document(path, layout)

            print("\n== a reload rebuilds the Empties from the document ==")
            written = read(path)
            open_document(path, layout)
            car = object_named("Car")
            empties = shapes.shape_empties(car, COLLIDER, "Shapes")
            check([json.loads(e[shapes.SHAPE_KEY])["shape"] for e in empties] == ["Sphere", "Capsule"],
                  "two Empties, sphere then capsule")
            check(sum(1 for o in bpy.data.objects if shapes.is_shape(o)) == 2,
                  "and no shape Empty from the previous load survives")
            save.save_prefab(bpy.context.scene)
            check(read(path) == written, "and saving the reload is a no-op")
    finally:
        paradise_assets.unregister()

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
