"""Schema-driven lamp previews follow document fields and transforms without serializing lamps."""

from __future__ import annotations

import json
import math
import sys
import tempfile
from pathlib import Path

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import paradise_assets
from paradise_assets import component_ops, edits, field_widgets
from paradise_assets.document import component_schema, prefab, project
from paradise_assets.materialize import light_preview, load, save, store

META = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
TRS = "7e55c210-3d41-4b8a-8f26-9c0a5e71b4d2"
ROOT = "a7fe95bd-d0bd-4529-a9f6-b87a9939e7e9"
POINT = "121e7c86-908a-4e9b-a22b-0fd8312b7edb"
SPOT = "c5279a26-c62b-42f8-a582-fb5d59f06f31"
COLOUR = "4b906a1f-303b-4669-ae96-69a4fd4e5325"
POWER = "6c296c08-90ca-4664-a150-1c548790bf3b"
CONE = "35364c1f-fd43-4cd6-9c2a-07e9fda9d20f"
PARAMS = "905cad7d-2f08-4a69-a7cf-d8b263dcc2e5"
SCHEMA = {
    "components": [
        {"id": POINT, "type": "Test.Omni", "previewLight": "Point", "fields": []},
        {"id": SPOT, "type": "Test.Headlight", "previewLight": "Spot", "fields": []},
        {
            "id": COLOUR,
            "type": "Test.Tint",
            "fields": [{"name": "Tint", "type": "color", "lightField": "Color"}],
        },
        {
            "id": POWER,
            "type": "Test.Power",
            "fields": [{"name": "Energy", "type": "float", "lightField": "Intensity", "default": 1}],
        },
        {
            "id": CONE,
            "type": "Test.Cone",
            "fields": [
                {"name": "Wide", "type": "float", "lightField": "OuterDegrees", "default": 60},
                {"name": "Core", "type": "float", "lightField": "InnerDegrees", "default": 30},
            ],
        },
        {
            "id": PARAMS,
            "type": "Test.Shape",
            "fields": [
                {"name": "Radius", "type": "float", "lightField": "Size", "default": 0.4},
                {"name": "Reach", "type": "float", "lightField": "Range", "default": 12},
                {"name": "Casts", "type": "bool", "lightField": "Shadows", "default": True},
            ],
        },
    ]
}


def component(id_, data="", type_="Test.Component"):
    return f'\n[[objects.components]]\nid = "{id_}"\ntype = "{type_}"\n{data}\n'


def object_(name, guid, parent="", position="[0, 0, 0]", rotation="[0, 0, 0, 1]", scale="[1, 1, 1]"):
    metadata = f'Guid = "{guid}"\nName = "{name}"\n'
    if parent:
        metadata += f'Parent = "{parent}"\n'
    return (
        "\n[[objects]]\n"
        + component(META, metadata, "meta")
        + component(TRS, f"Position = {position}\nRotation = {rotation}\nScale = {scale}", "transform")
    )


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("PASS " + message)


def near(a, b):
    return (Vector(a) - Vector(b)).length < 1e-4


def main():
    paradise_assets.register()
    try:
        with tempfile.TemporaryDirectory(prefix="paradise-light-preview-") as directory:
            root = Path(directory)
            (root / "assets/levels").mkdir(parents=True)
            (root / ".editor").mkdir()
            (root / "assets/project.toml").write_text('schema_version = 1\nname = "lights"\n')
            (root / ".editor/authoring-schema.json").write_text(json.dumps(SCHEMA))
            point_guid = "7d9c2282-c922-4605-9c4a-6b26fba2390c"
            spot_guid = "2b810fab-33fc-4bda-89b1-bd6f7d9e28d3"
            text = "schema_version = 1\n" + object_(
                "Root",
                ROOT,
                position="[2, 0, 1]",
                rotation="[0, 0.7071067811865475, 0, 0.7071067811865476]",
                scale="[2, 1, 3]",
            )
            text += object_("Point", point_guid, ROOT, position="[0, 3, 1]")
            text += component(POINT) + component(POWER, "Energy = 3") + component(PARAMS)
            text += component(COLOUR, "[objects.components.Tint]\nr = 1\ng = 0.5\nb = 0.25\na = 1")
            text += object_(
                "Spot",
                spot_guid,
                ROOT,
                position="[-1, 5, 0]",
                rotation="[-0.7071067811865475, 0, 0, 0.7071067811865476]",
            )
            text += component(SPOT) + component(POWER, "Energy = 6") + component(CONE) + component(PARAMS)
            path = root / "assets/levels/lights.prefab"
            # Canonical input makes a byte comparison about authoring, not formatting.
            path.write_text(prefab.dumps(prefab.loads(text, str(path))))
            layout = project.locate(str(path))
            scene = bpy.context.scene
            load.load_document(
                scene, prefab.loads(path.read_text(), str(path)), str(path), layout, clear_startup=True
            )
            scene.view_layers[0].update()
            point = store.object_with_guid(scene, point_guid)
            spot = store.object_with_guid(scene, spot_guid)
            pp = next(child for child in point.children if light_preview.is_preview(child))
            sp = next(child for child in spot.children if light_preview.is_preview(child))
            check(
                pp.type == "LIGHT" and pp.data.type == "POINT", "point archetype materializes a native lamp"
            )
            check(sp.data.type == "SPOT", "spot archetype materializes a cone")
            check(
                store.guid_of(pp) is None and store.guid_of(sp) is None,
                "preview lamps have no document identity",
            )
            check(
                near(pp.matrix_world.translation, (5, -1, 3)), "parented point position matches contract axes"
            )
            check(
                near(sp.matrix_world.translation, (2, -3, 5)), "parented spot position matches contract axes"
            )
            check(
                near(sp.matrix_world.to_3x3() @ Vector((0, 0, -1)), (0, 0, -1)),
                "spot's -Z aim faces authored ground",
            )
            check(
                near(sp.matrix_world.to_scale(), (1, 1, 1)),
                "nonuniform parent scale cannot stretch cone or range",
            )
            check(pp.data.energy == 300 and sp.data.energy == 600, "punctual intensity uses 100 W per unit")
            check(near(pp.data.color, (1, 0.21404114, 0.05087609)), "sRGB colour is decoded for native light")
            check(
                abs(sp.data.spot_size - math.radians(60)) < 1e-5 and sp.data.spot_blend == 0.5,
                "cone angles map to full cone and penumbra",
            )
            check(
                sp.data.use_shadow and abs(sp.data.shadow_soft_size - 0.4) < 1e-5,
                "shadow intent and emitter radius appear",
            )
            before = path.read_bytes()
            save.save_prefab(scene)
            check(path.read_bytes() == before, "untouched save is byte-identical and adds no lamp objects")

            # Opening another document uses clear_startup too, even before a .blend is saved.
            load.load_document(
                scene, prefab.loads(path.read_text(), str(path)), str(path), layout, clear_startup=True
            )
            check(sum(light_preview.is_preview(obj) for obj in scene.objects) == 2,
                  "reopening an unsaved document replaces lamp helpers without retaining freed objects")
            point = store.object_with_guid(scene, point_guid)
            spot = store.object_with_guid(scene, spot_guid)
            pp = next(child for child in point.children if light_preview.is_preview(child))
            sp = next(child for child in spot.children if light_preview.is_preview(child))

            bpy.context.view_layer.objects.active = point
            point.select_set(True)
            vocabulary = component_schema.load(str(root))
            colour_field = vocabulary.get(COLOUR).fields[0]
            field_widgets.sync(
                bpy.context,
                point,
                [
                    (
                        COLOUR,
                        vocabulary.get(COLOUR).plan({"Tint": {}})[0],
                        {"r": 1, "g": 0.5, "b": 0.25, "a": 1},
                    )
                ],
            )
            slot = bpy.context.window_manager.paradise_field_slots[0]
            check(
                near(slot.value_color[:3], (1, 0.5, 0.25)),
                "colour widget reads RGBA object without resetting it to white",
            )
            slot.value_color = (0.2, 0.4, 0.6, 1)
            changed = edits.edited_fields(point, COLOUR)["Tint"]
            check(
                isinstance(changed, dict) and set(changed) == set("rgba"),
                "colour widget writes engine RGBA object shape",
            )
            check(
                pp.data.color[2] > pp.data.color[0], "native preview updates from colour widget immediately"
            )
            edits.set_field(spot, POWER, "Energy", 9)
            edits.set_field(spot, CONE, "Wide", 80)
            edits.set_field(spot, CONE, "Core", 20)
            component_ops._redraw(bpy.context)
            check(
                sp.data.energy == 900 and abs(sp.data.spot_blend - 0.75) < 1e-5,
                "component edits update brightness and cone live",
            )
            spot.location.x += 2
            scene.view_layers[0].update()
            check(
                near(sp.matrix_world.translation, spot.matrix_world.translation),
                "moving the object moves its preview",
            )
            save.save_prefab(scene)
            saved = prefab.loads(path.read_text(), str(path))
            check(len(saved.objects) == 3, "save keeps only canonical document objects")
            tint = next(obj for obj in saved.objects if obj.guid == point_guid).component(COLOUR).data["Tint"]
            check(
                set(tint) == set("rgba") and abs(tint["g"] - 0.4) < 1e-5,
                "saved colour remains readable by generated game reader",
            )
            load.load_document(scene, saved, str(path), layout)
            previews = [obj for obj in scene.collection.all_objects if light_preview.is_preview(obj)]
            check(len(previews) == 2, "reload replaces previews without accumulating duplicates")
            spot = store.object_with_guid(scene, spot_guid)
            edits.remove_component(spot, SPOT)
            light_preview.refresh(scene)
            check(
                not any(light_preview.is_preview(child) for child in spot.children),
                "removing archetype removes its light contribution",
            )
            check(colour_field.editable, "preview metadata leaves canonical component editing enabled")
    finally:
        paradise_assets.unregister()


if __name__ == "__main__":
    main()
