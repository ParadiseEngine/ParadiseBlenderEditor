"""Blender integration coverage for entity references (#70, #71).

    blender --background --factory-startup --python tests/integration/test_entity_refs.py

A level camera's ``CameraTarget.Object`` (``authoredBy: entity``) is picked -- from the selection
and from the search list -- as the level's own objects, an instance root and a child of a placed
prefab; the save writes the resolved guid, refuses a value that names nothing, and only warns
about a dangling value a placed prefab contributes.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import paradise_assets
from paradise_assets import edits, field_widgets
from paradise_assets.document import prefab, project, resolve, well_known
from paradise_assets.materialize import load, save, store

failures: list[str] = []

LEVEL_ROOT = "11111111-0000-4000-8000-000000000001"
CAMERA = "11111111-0000-4000-8000-000000000002"
SHOP = "11111111-0000-4000-8000-000000000003"
SHOP_ROOT = "22222222-0000-4000-8000-000000000001"
DOOR = "22222222-0000-4000-8000-000000000002"
SHOP_PREFAB = "33333333-0000-4000-8000-000000000001"

TARGET_ID = "9519caf9-7a87-4f72-845d-d59695cac8b9"
ROUTE_ID = "aaaaaaaa-0000-4000-8000-0000000000aa"
ZERO = "00000000-0000-0000-0000-000000000000"
MINTED_DOOR = resolve.mint_child_guid(SHOP, DOOR)


def check(condition: bool, label: str) -> bool:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)
    return condition


def _meta(guid: str, name: str, parent: str | None = None) -> str:
    text = (f'[[objects.components]]\nid = "{well_known.META_ID}"\ntype = "meta"\n'
            f'Guid = "{guid}"\nName = "{name}"\n')
    if parent:
        text += f'Parent = "{parent}"\n'
    return text + "\n"


_TRANSFORM = (f'[[objects.components]]\nid = "{well_known.TRANSFORM_ID}"\ntype = "transform"\n'
              "Position = [0, 0, 0]\nRotation = [0, 0, 0, 1]\nScale = [1, 1, 1]\n\n")


def make_project(root: Path, *, door_route: str | None = None) -> Path:
    assets = root / "assets"
    (assets / "levels").mkdir(parents=True, exist_ok=True)
    (assets / "prefabs").mkdir(parents=True, exist_ok=True)
    (root / ".editor").mkdir(parents=True, exist_ok=True)
    (assets / "project.toml").write_text('schema_version = 1\nname = "refs"\n', encoding="utf-8")
    (root / ".editor" / "authoring-schema.json").write_text(json.dumps({
        "components": [
            {"id": TARGET_ID, "type": "ShiningPie.Authoring.CameraTarget",
             "displayName": "Camera target",
             "fields": [{"name": "Object", "type": "string", "authoredBy": "entity"}]},
            {"id": ROUTE_ID, "type": "Game.Route", "displayName": "Route",
             "fields": [{"name": "Stops", "type": "array",
                         "items": {"name": "Stops", "type": "string", "authoredBy": "entity"}}]},
        ]
    }), encoding="utf-8")

    door = "[[objects]]\n\n" + _meta(DOOR, "Door", SHOP_ROOT) + _TRANSFORM
    if door_route is not None:
        door += (f'[[objects.components]]\nid = "{ROUTE_ID}"\ntype = "Game.Route"\n'
                 f'Stops = ["{door_route}"]\n\n')
    (assets / "prefabs" / "shop.prefab").write_text(
        "schema_version = 1\n\n[[objects]]\n\n" + _meta(SHOP_ROOT, "Shop") + _TRANSFORM + door,
        encoding="utf-8")
    (assets / "prefabs" / "shop.prefab.meta").write_text(
        f'schema_version = 1\nguid = "{SHOP_PREFAB}"\n', encoding="utf-8")

    level = assets / "levels" / "street.prefab"
    level.write_text(
        "schema_version = 1\n\n"
        "[[objects]]\n\n" + _meta(LEVEL_ROOT, "Street") + _TRANSFORM
        + "[[objects]]\n\n" + _meta(CAMERA, "Camera", LEVEL_ROOT) + _TRANSFORM
        + f'[[objects.components]]\nid = "{TARGET_ID}"\ntype = "ShiningPie.Authoring.CameraTarget"\n'
        f'Object = "{MINTED_DOOR}"\n\n'
        f'[[objects]]\nprefab = {{ guid = "{SHOP_PREFAB}", path = "prefabs/shop.prefab" }}\n\n'
        + _meta(SHOP, "Shop", LEVEL_ROOT) + _TRANSFORM,
        encoding="utf-8")
    return level


def open_level(level: Path, layout) -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    loaded = load.load_document(
        bpy.context.scene, prefab.loads(level.read_text(encoding="utf-8"), str(level)), str(level), layout)
    check(loaded.derived >= 1 and not loaded.warnings,
          f"the level loads with the shop expanded ({loaded.derived}, {loaded.warnings})")


def camera_target(level: Path) -> object:
    written = prefab.loads(level.read_text(encoding="utf-8"), str(level))
    camera = next(entry for entry in written.objects if entry.guid == CAMERA)
    return camera.component(TARGET_ID).data.get("Object")


def select_only(*objects, active) -> None:
    for obj in bpy.context.scene.objects:
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = active


def refusal_of(scene) -> str | None:
    try:
        save.save_prefab(scene)
    except save.SaveError as error:
        return str(error)
    return None


def main() -> int:
    paradise_assets.register()
    try:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            level = make_project(root)
            layout = project.ProjectLayout(str(root))
            scene_of = lambda: bpy.context.scene  # noqa: E731

            print("\n== a reference to a placed prefab's child loads and saves byte-identically ==")
            open_level(level, layout)
            before = level.read_text(encoding="utf-8")
            result = save.save_prefab(scene_of())
            check(level.read_text(encoding="utf-8") == before, "an unchanged save rewrites nothing")
            check(not result.warnings, f"and warns about nothing ({result.warnings})")
            camera = store.object_with_guid(scene_of(), CAMERA)
            door = store.object_with_guid(scene_of(), MINTED_DOOR)
            shop = store.object_with_guid(scene_of(), SHOP)
            check(door is not None and store.is_derived(door), "the door is the shop's resolved child")
            check(field_widgets.entity_label(door).startswith(door.name)
                  and "(" in field_widgets.entity_label(door),
                  f"the picker names the door by where it lives ({field_widgets.entity_label(door)})")

            print("\n== the eyedropper names the other selected object ==")
            select_only(shop, camera, active=camera)
            outcome = bpy.ops.paradise_assets.pick_entity(
                component_id=TARGET_ID, field_name="Object", from_selection=True)
            check(outcome == {"FINISHED"}, f"the pick finished ({outcome})")
            save.save_prefab(scene_of())
            check(camera_target(level) == SHOP, f"the instance root is written ({camera_target(level)})")

            select_only(camera, active=camera)
            outcome = bpy.ops.paradise_assets.pick_entity(
                component_id=TARGET_ID, field_name="Object", from_selection=True)
            check(outcome == {"CANCELLED"}, "with nothing else selected the eyedropper refuses")

            print("\n== the search list offers resolved children and writes their guid ==")
            select_only(camera, active=camera)
            items = field_widgets._pick_entity_items(
                type("Probe", (), {"current_guid": ""})(), bpy.context)
            idents = {item[0] for item in items}
            check({MINTED_DOOR, SHOP, LEVEL_ROOT, CAMERA} <= idents,
                  "the list holds entries, the instance root and its resolved child")
            outcome = bpy.ops.paradise_assets.pick_entity(
                "EXEC_DEFAULT", component_id=TARGET_ID, field_name="Object", entity=MINTED_DOOR)
            check(outcome == {"FINISHED"}, f"the search pick finished ({outcome})")
            save.save_prefab(scene_of())
            check(camera_target(level) == MINTED_DOOR, "the child's resolved guid is written")
            check(level.read_text(encoding="utf-8") == before, "and the file is back to what it was")

            print("\n== (none) writes the zero guid, which the reader parses ==")
            outcome = bpy.ops.paradise_assets.pick_entity(
                "EXEC_DEFAULT", component_id=TARGET_ID, field_name="Object", entity="NONE")
            save.save_prefab(scene_of())
            check(camera_target(level) == ZERO, f"cleared to Guid.Empty ({camera_target(level)})")

            print("\n== a value naming nothing is refused, and the document is left alone ==")
            edits.set_field(camera, TARGET_ID, "Object", DOOR)   # the prefab-LOCAL guid
            written = level.read_text(encoding="utf-8")
            refused = refusal_of(scene_of())
            check(refused is not None and DOOR in refused and "Camera" in refused,
                  f"a prefab-local guid is refused by name ({refused})")
            check(level.read_text(encoding="utf-8") == written, "and nothing was written")
            check(edits.count(camera) == 1, "and the pending edit survives to be fixed")

            edits.set_field(camera, TARGET_ID, "Object", "Player")
            refused = refusal_of(scene_of())
            check(refused is not None and "not an object identity" in refused,
                  f"a name is refused as no identity ({refused})")

            print("\n== deleting the referenced instance is an error, not a silent guid ==")
            edits.set_field(camera, TARGET_ID, "Object", MINTED_DOOR)
            save.save_prefab(scene_of())
            for obj in list(scene_of().objects):
                if obj is shop or obj.parent is shop:
                    bpy.data.objects.remove(obj, do_unlink=True)
            refused = refusal_of(scene_of())
            check(refused is not None and MINTED_DOOR in refused,
                  f"the dangling child reference is refused ({refused})")

            print("\n== a dangling value the placed prefab contributes only warns ==")
            with tempfile.TemporaryDirectory() as other_root:
                warned = make_project(Path(other_root), door_route=DOOR)
                open_level(warned, project.ProjectLayout(other_root))
                result = save.save_prefab(scene_of())
                check(any("Route.Stops.0" in w and "placed prefab" in w for w in result.warnings),
                      f"the save warns and names the prefab's field ({result.warnings})")
    finally:
        paradise_assets.unregister()

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
