"""Blender integration coverage for Bezier curves as document objects (#76).

    blender --background --factory-startup --python tests/integration/test_curves.py

A CURVE object admitted into a level bakes its Bezier splines into a ``HostSpline`` field (knots
plus relative handles, document axes, object space), keeps the row's game members, round-trips
through save and reload at a byte-exact fixed point, refuses non-Bezier splines by name, re-mints
on duplication, and a curve nobody admitted stays Blender's own.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import paradise_assets
from paradise_assets import duplicates, edits
from paradise_assets.document import prefab, project, resolve, well_known
from paradise_assets.document import splines as splines_doc
from paradise_assets.document.asset_reference import AssetReference
from paradise_assets.materialize import load, save, store

failures: list[str] = []

ROOT = "44444444-0000-4000-8000-000000000001"
ROUTE_ID = "bbbbbbbb-0000-4000-8000-0000000000bb"

SPLINE_ROW = {"type": "object", "authoredBy": "spline", "fields": [
    {"name": "Knots", "type": "array", "items": {"type": "object", "fields": [
        {"name": "Position", "type": "vector3"},
        {"name": "HandleIn", "type": "vector3"},
        {"name": "HandleOut", "type": "vector3"},
    ]}},
    {"name": "Cyclic", "type": "bool"},
]}


def check(condition: bool, label: str) -> bool:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)
    return condition


def make_project(root: Path) -> Path:
    assets = root / "assets"
    (assets / "levels").mkdir(parents=True)
    (root / ".editor").mkdir(parents=True)
    (assets / "project.toml").write_text('schema_version = 1\nname = "curves"\n', encoding="utf-8")
    (root / ".editor" / "authoring-schema.json").write_text(json.dumps({"components": [{
        "id": ROUTE_ID, "type": "Game.PatrolRoute", "displayName": "Patrol route",
        "fields": [{"name": "Legs", "type": "array", "items": {
            "name": "Legs", "type": "object",
            "fields": [dict(SPLINE_ROW, name="Curve"), {"name": "Speed", "type": "float"}]}}],
    }]}), encoding="utf-8")
    level = assets / "levels" / "yard.prefab"
    level.write_text(
        "schema_version = 1\n\n[[objects]]\n\n"
        f'[[objects.components]]\nid = "{well_known.META_ID}"\ntype = "meta"\n'
        f'Guid = "{ROOT}"\nName = "Yard"\n\n'
        f'[[objects.components]]\nid = "{well_known.TRANSFORM_ID}"\ntype = "transform"\n'
        "Position = [1, 0, 0]\nRotation = [0, 0, 0, 1]\nScale = [1, 1, 1]\n",
        encoding="utf-8")
    return level


def open_level(level: Path, layout) -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    load.load_document(
        bpy.context.scene, prefab.loads(level.read_text(encoding="utf-8"), str(level)), str(level), layout)


def new_curve(name: str, points, *, kind: str = "BEZIER", location=(0.0, 0.0, 0.0)):
    data = bpy.data.curves.new(name, "CURVE")
    data.dimensions = "3D"
    add_spline(data, points, kind=kind)
    obj = bpy.data.objects.new(name, data)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    return obj


def add_spline(data, points, *, kind: str = "BEZIER", cyclic: bool = False):
    spline = data.splines.new(kind)
    if kind == "BEZIER":
        spline.bezier_points.add(len(points) - 1)
        for point, (co, left, right) in zip(spline.bezier_points, points, strict=True):
            point.handle_left_type = point.handle_right_type = "FREE"
            point.co, point.handle_left, point.handle_right = co, left, right
    else:
        spline.points.add(len(points) - 1)
        for point, (co, _left, _right) in zip(spline.points, points, strict=True):
            point.co = (*co, 1.0)
    spline.use_cyclic_u = cyclic
    return spline


def route_of(level: Path, guid: str):
    written = prefab.loads(level.read_text(encoding="utf-8"), str(level))
    entry = next((e for e in written.objects if e.guid == guid), None)
    if entry is None:
        return None
    component = entry.component(ROUTE_ID)
    return component.data if component is not None else None


def select_only(obj) -> None:
    for other in bpy.context.scene.objects:
        other.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


POINTS = [
    ((0.0, 0.0, 0.0), (-1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
    ((4.0, 2.0, 0.0), (3.0, 2.0, 0.0), (5.0, 2.0, 1.0)),
    ((8.0, 0.0, 0.5), (7.5, 0.0, 0.5), (8.5, 0.0, 0.5)),
]


TRACK = "55555555-0000-4000-8000-000000000001"
TRACK_RAIL = "55555555-0000-4000-8000-000000000002"
INSTANCE = "44444444-0000-4000-8000-000000000009"


def prefab_curve_section(root: Path) -> None:
    """A curve a placed prefab contributes shows as a curve, and editing it here is refused:
    the save bakes only what the level's own entries author."""
    print("\n== a prefab's curve is shown, and editing it in the level is refused ==")
    level = make_project(root)
    row = splines_doc.from_blender("Rail", POINTS, False)

    def entry(guid, name, parent=None, *components, reference=None):
        made = prefab.PrefabObject.with_meta(guid, name, parent)
        made.prefab = reference
        made.components.append(prefab.PrefabComponent(
            well_known.TRANSFORM_ID, "transform",
            {"Position": [0.0, 0.0, 0.0], "Rotation": [0.0, 0.0, 0.0, 1.0], "Scale": [1.0, 1.0, 1.0]}))
        made.components.extend(components)
        return made

    track = prefab.PrefabDocument([
        entry(TRACK, "Track"),
        entry(TRACK_RAIL, "Rail", TRACK, prefab.PrefabComponent(
            ROUTE_ID, "Game.PatrolRoute", {"Legs": [{"Curve": row, "Speed": 1.0}]})),
    ])
    (root / "assets" / "prefabs").mkdir(parents=True, exist_ok=True)
    (root / "assets" / "prefabs" / "track.prefab").write_text(prefab.dumps(track), encoding="utf-8")
    (root / "assets" / "prefabs" / "track.prefab.meta").write_text(
        'schema_version = 1\nguid = "66666666-0000-4000-8000-000000000001"\n', encoding="utf-8")
    document = prefab.loads(level.read_text(encoding="utf-8"), str(level))
    document.objects.append(entry(
        INSTANCE, "Track", ROOT,
        reference=AssetReference("66666666-0000-4000-8000-000000000001", "prefabs/track.prefab")))
    level.write_text(prefab.dumps(document), encoding="utf-8")

    open_level(level, project.ProjectLayout(str(root)))
    derived = store.object_with_guid(bpy.context.scene, resolve.mint_child_guid(INSTANCE, TRACK_RAIL))
    check(derived is not None and derived.type == "CURVE" and store.is_derived(derived),
          "the prefab's rail is a curve in the level")
    save.save_prefab(bpy.context.scene)
    check(prefab.loads(level.read_text(encoding="utf-8"), str(level)).objects[-1].guid == INSTANCE,
          "an untouched prefab curve saves")
    before = level.read_text(encoding="utf-8")
    derived.data.splines[0].bezier_points[1].co.z += 2.0
    refused = None
    try:
        save.save_prefab(bpy.context.scene)
    except save.SaveError as error:
        refused = str(error)
    check(refused is not None and "comes from its prefab" in refused,
          f"a knot moved on it is refused, not dropped ({refused})")
    check(level.read_text(encoding="utf-8") == before, "and nothing is written")


def main() -> int:
    paradise_assets.register()
    try:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            level = make_project(root)
            layout = project.ProjectLayout(str(root))
            scene = lambda: bpy.context.scene  # noqa: E731

            print("\n== a curve nobody admitted stays Blender's own ==")
            open_level(level, layout)
            stray = new_curve("Stray", POINTS)
            save.save_prefab(scene())
            written = prefab.loads(level.read_text(encoding="utf-8"), str(level))
            check(len(written.objects) == 1 and store.guid_of(stray) is None,
                  "the save wrote no entry for it")

            print("\n== Add Curve to Document admits it, and the save bakes its splines ==")
            yard = store.object_with_guid(scene(), ROOT)
            rail = new_curve("Rail", POINTS, location=(3.0, 0.0, 0.0))
            # Ctrl+P keeps it in place through the parent inverse; the save must fold that.
            rail.parent = yard
            rail.matrix_parent_inverse = yard.matrix_world.inverted()
            bpy.context.view_layer.update()
            world_before = rail.matrix_world.copy()
            select_only(rail)
            outcome = bpy.ops.paradise_assets.admit_curve(component=ROUTE_ID)
            check(outcome == {"FINISHED"} and store.guid_of(rail) is not None,
                  f"the curve is a document object now ({outcome})")
            save.save_prefab(scene())
            guid = store.guid_of(rail)
            data = route_of(level, guid)
            legs = data.get("Legs") if data else None
            check(isinstance(legs, list) and len(legs) == 1, f"one row per spline ({legs})")
            knots = legs[0]["Curve"]["Knots"] if legs else []
            check([k["Position"] for k in knots] == [[0.0, 0.0, 0.0], [4.0, 0.0, -2.0], [8.0, 0.5, 0.0]],
                  f"knots are object-space positions in document axes ({[k['Position'] for k in knots]})")
            check(knots and knots[1]["HandleIn"] == [-1.0, 0.0, 0.0]
                  and knots[1]["HandleOut"] == [1.0, 1.0, 0.0],
                  f"handles are offsets from their knot ({knots[1] if len(knots) > 1 else None})")
            check(legs and legs[0]["Curve"]["Cyclic"] is False and legs[0]["Speed"] == 0.0,
                  "the cyclic flag is written and the game member gets its default")
            check((rail.matrix_world.translation - world_before.translation).length < 1e-5,
                  "folding the parent inverse did not move the curve")

            before = level.read_text(encoding="utf-8")
            save.save_prefab(scene())
            check(level.read_text(encoding="utf-8") == before, "an unchanged save rewrites nothing")

            print("\n== a reload rebuilds the curve from the document ==")
            open_level(level, layout)
            rail = store.object_with_guid(scene(), guid)
            check(rail is not None and rail.type == "CURVE", "the object comes back as a curve")
            spline = rail.data.splines[0] if rail is not None and rail.data.splines else None
            check(spline is not None and spline.type == "BEZIER" and len(spline.bezier_points) == 3,
                  "with its one Bezier spline of three knots")
            if spline is not None:
                check((spline.bezier_points[1].co - Vector(POINTS[1][0])).length < 1e-5
                      and (spline.bezier_points[1].handle_right - Vector(POINTS[1][2])).length < 1e-5,
                      "in Blender's axes again, handles absolute")
                check(spline.bezier_points[0].handle_left_type == "ALIGNED",
                      "collinear handles come back aligned")
            moved = (rail.matrix_world.translation - world_before.translation).length if rail else 1.0
            check(moved < 1e-5, "where it was")
            save.save_prefab(scene())
            check(level.read_text(encoding="utf-8") == before, "a save after reload rewrites nothing")

            print("\n== the row's game members survive, rows follow the splines ==")
            edits.set_field(rail, ROUTE_ID, "Legs/0/Speed", 2.5)
            add_spline(rail.data, [((0, 0, 0), (0, -1, 0), (0, 1, 0)),
                                   ((0, 5, 0), (0, 4, 0), (0, 6, 0))], cyclic=True)
            save.save_prefab(scene())
            legs = route_of(level, guid)["Legs"]
            shown = [(r["Speed"], r["Curve"]["Cyclic"]) for r in legs]
            check(len(legs) == 2 and legs[0]["Speed"] == 2.5 and legs[1]["Curve"]["Cyclic"] is True,
                  f"speed kept on row 0, a cyclic row 1 added ({shown})")
            check(legs[0]["Curve"]["Knots"] == knots, "the untouched spline keeps its numbers")

            rail.data.splines.remove(rail.data.splines[1])
            save.save_prefab(scene())
            check(len(route_of(level, guid)["Legs"]) == 1, "deleting a spline drops its row")

            print("\n== deleting the FIRST spline keeps each row with its own spline ==")
            second = [((0, 0, 3), (0, -1, 3), (0, 1, 3)), ((0, 5, 3), (0, 4, 3), (0, 6, 3))]
            add_spline(rail.data, second)
            save.save_prefab(scene())
            edits.set_field(rail, ROUTE_ID, "Legs/1/Speed", 8.5)
            save.save_prefab(scene())
            second_knots = route_of(level, guid)["Legs"][1]["Curve"]["Knots"]
            rail.data.splines.remove(rail.data.splines[0])
            save.save_prefab(scene())
            legs = route_of(level, guid)["Legs"]
            check(len(legs) == 1 and legs[0]["Speed"] == 8.5
                  and legs[0]["Curve"]["Knots"] == second_knots,
                  f"the surviving spline keeps its own speed ({[r['Speed'] for r in legs]})")

            print("\n== removed AND reshaped since the last save is refused, not guessed ==")
            add_spline(rail.data, POINTS)
            save.save_prefab(scene())
            rail.data.splines.remove(rail.data.splines[0])
            knot = rail.data.splines[0].bezier_points[0]
            knot.co.x += 1.0
            refused = None
            try:
                save.save_prefab(scene())
            except save.SaveError as error:
                refused = str(error)
            check(refused is not None and "cannot be told" in refused,
                  f"the ambiguous save refuses ({refused})")
            knot.co.x -= 1.0
            save.save_prefab(scene())
            check(len(route_of(level, guid)["Legs"]) == 1, "undoing the reshape lets it save")

            print("\n== a spline added after a deletion is new, not the deleted one's heir ==")
            edits.set_field(rail, ROUTE_ID, "Legs/0/Speed", 4.0)
            add_spline(rail.data, second)
            save.save_prefab(scene())
            edits.set_field(rail, ROUTE_ID, "Legs/1/Speed", 9.0)
            save.save_prefab(scene())
            rail.data.splines.remove(rail.data.splines[0])
            add_spline(rail.data, [((2, 0, 0), (1, 0, 0), (3, 0, 0)), ((2, 5, 0), (1, 5, 0), (3, 5, 0))])
            save.save_prefab(scene())
            speeds = [row["Speed"] for row in route_of(level, guid)["Legs"]]
            check(speeds == [9.0, 0.0], f"the kept spline keeps 9.0, the new one starts fresh ({speeds})")

            print("\n== a poly spline is refused, naming the object ==")
            add_spline(rail.data, POINTS, kind="POLY")
            written = level.read_text(encoding="utf-8")
            refused = None
            try:
                save.save_prefab(scene())
            except save.SaveError as error:
                refused = str(error)
            check(refused is not None and "Rail" in refused and "only Bezier" in refused,
                  f"the save refuses ({refused})")
            check(level.read_text(encoding="utf-8") == written, "and writes nothing")
            rail.data.splines.remove(rail.data.splines[1])

            print("\n== duplication re-mints, deletion leaves no entry ==")
            # The depsgraph handler's pass, seeded before the copy exists (a background Blender
            # pumps no timers): it is what tells the copy from the original.
            duplicates.settle()
            copy = rail.copy()
            copy.data = rail.data.copy()
            scene().collection.objects.link(copy)
            bpy.context.view_layer.update()
            duplicates.settle()
            save.save_prefab(scene())
            check(store.guid_of(copy) not in (None, guid), "the copy has an identity of its own")
            check(route_of(level, store.guid_of(copy)) is not None, "and is written with its route")
            copy_guid = store.guid_of(copy)
            bpy.data.objects.remove(copy, do_unlink=True)
            save.save_prefab(scene())
            check(route_of(level, copy_guid) is None and route_of(level, guid) is not None,
                  "deleting the copy removes its entry and keeps the original's")
        with tempfile.TemporaryDirectory() as raw_root:
            prefab_curve_section(Path(raw_root))
    except Exception as error:
        # On stdout and last: the runner keeps only the tail of the output, and a traceback on
        # stderr lands at its top.
        traceback.print_exc(file=sys.stdout)
        check(False, f"the test raised {error!r}")
    finally:
        paradise_assets.unregister()

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
