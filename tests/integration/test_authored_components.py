"""Schema-driven authored components, end to end inside Blender.

Writes a game-style ``authoring-schema.json``, attaches components to entities through the
same module the UI uses, exports, and checks the resulting ``Components.Custom`` payloads --
plus the behaviours a unit test cannot see: that ID properties keep their schema types through
Blender's property system, that the schema hot-reloads when the file changes, and that a
component the schema no longer declares is dropped from the export (with a warning) rather
than exported stale.

The document lands in ``$TMPDIR/paradise_export_test`` so ``tools/run_tests.sh`` feeds it to
the .NET conformance gate along with the other exports.

Run with::

    blender --background --factory-startup --python tests/integration/test_authored_components.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

import bpy  # noqa: E402

import paradise_blender  # noqa: E402
from paradise_blender.authoring import authored_components as authored  # noqa: E402
from paradise_blender.contract import authoring as contract_authoring  # noqa: E402
from paradise_blender.export.scene import export_scene  # noqa: E402

DATA_DIR = os.path.join(tempfile.gettempdir(), "paradise_export_test")

failures: list[str] = []


def check(condition: bool, description: str, detail: str = "") -> None:
    if condition:
        print(f"ok   {description}")
    else:
        print(f"FAIL {description}{(' — ' + detail) if detail else ''}")
        failures.append(description)


SCHEMA = {
    "version": 2,
    "components": [
        {
            "id": "game.creature",
            "displayName": "Creature",
            "fields": [
                {"name": "MaxSpeed", "type": "float", "minimum": 0.1, "maximum": 50, "default": 7},
                {"name": "Lives", "type": "int", "default": 3},
                {"name": "Friendly", "type": "bool", "default": True},
                {"name": "Nickname", "type": "string"},
                {"name": "Mode", "type": "enum", "values": ["Idle", "Chase", "Flee"], "default": "Chase"},
                {"name": "Home", "type": "vector3", "default": [1, 2, 3]},
                {"name": "Tint", "type": "color", "default": {"r": 1, "g": 0.5, "b": 0, "a": 1}},
                {
                    "name": "Box",
                    "type": "object",
                    "fields": [
                        {"name": "SizeX", "type": "float", "default": 1},
                        {"name": "SizeY", "type": "float", "default": 2},
                    ],
                },
                {
                    "name": "Shape",
                    "type": "object",
                    "authoredBy": "shape",
                    "fields": [{"name": "SizeX", "type": "float"}],
                },
            ],
        },
        {
            "id": "game.marker",
            "displayName": "Marker",
            "fields": [{"name": "Label", "type": "string", "default": "spawn"}],
        },
    ],
}


def write_schema(document: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(contract_authoring.schema_path(DATA_DIR), "w", encoding="utf-8") as file:
        json.dump(document, file)


def build_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.paradise_project.data_dir = DATA_DIR
    scene.paradise_project.scene_name_override = "authored_test"
    scene.paradise_project.export_on_save = False

    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0))
    creature = bpy.context.active_object
    creature.name = "Creature"
    creature.paradise.is_entity = True

    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(2, 0, 0))
    plain = bpy.context.active_object
    plain.name = "Plain"
    plain.paradise.is_entity = True

    # One lamp OWNED by an entity, one scene-level lamp: the export must route them apart.
    bpy.ops.object.light_add(type="SUN", location=(0, 0, 10))
    sun = bpy.context.active_object
    sun.name = "OwnedSun"
    sun.data.energy = 5.0
    sun.paradise.is_entity = True

    bpy.ops.object.light_add(type="POINT", location=(3, 0, 4))
    fill = bpy.context.active_object
    fill.name = "SceneFill"


def exported_entities() -> dict[str, dict]:
    path = os.path.join(DATA_DIR, "scenes", "authored_test.json")
    with open(path, encoding="utf-8") as file:
        document = json.load(file)
    return {entity["Id"]: entity for entity in document["Entities"]}


def main() -> int:
    paradise_blender.register()
    write_schema(SCHEMA)
    build_scene()

    creature_obj = bpy.data.objects["Creature"]
    document = authored.schema_for_data_dir(DATA_DIR)
    check(authored.schema_load_error(DATA_DIR) is None, "the schema file loads")
    game_ids = {c.id for c in document.components if c.id.startswith("game.")}
    check(game_ids == {"game.creature", "game.marker"}, "both game components are read")
    check(
        authored.component_by_id(document, "paradise.rigidbody") is not None,
        "the engine's own schema is merged in",
    )
    check(
        not authored.is_authorable(authored.component_by_id(document, "paradise.renderable"))
        and not authored.is_authorable(authored.component_by_id(document, "paradise.light"))
        and authored.is_authorable(authored.component_by_id(document, "paradise.agent")),
        "host-owned and host-baked engine components are not offered; plain ones are",
    )

    # -- storage ------------------------------------------------------------------------
    component = authored.component_by_id(document, "game.creature")
    authored.enable_component(creature_obj, component)
    check(
        authored.enabled_component_ids(creature_obj) == ["game.creature"],
        "enabling records the component id",
    )

    friendly_key = authored.value_key("game.creature", "Friendly")
    check(
        isinstance(creature_obj[friendly_key], bool),
        "a bool field is stored as a real bool ID property",
        f"stored type: {type(creature_obj[friendly_key]).__name__}",
    )
    check(
        creature_obj[authored.value_key("game.creature", "Mode")] == "Chase",
        "an enum field starts on its declared default member",
    )

    creature_obj[authored.value_key("game.creature", "MaxSpeed")] = 9.25
    creature_obj[authored.value_key("game.creature", "Mode")] = "Flee"
    creature_obj[authored.value_key("game.creature", "Friendly")] = False
    creature_obj[authored.value_key("game.creature", "Box/SizeX")] = 4.0

    # -- export -------------------------------------------------------------------------
    export_scene(bpy.context.scene)
    entities = exported_entities()

    custom = entities["Creature"]["Components"].get("Custom")
    check(custom is not None and len(custom) == 1, "the authored component is exported")
    payload = custom[0]["Data"]
    check(custom[0]["Id"] == "game.creature", "under its schema id")
    check(payload["MaxSpeed"] == 9.25, "an authored float value survives")
    check(payload["Lives"] == 3, "an untouched field exports its schema default")
    check(payload["Friendly"] is False, "a bool exports as JSON bool")
    check(payload["Mode"] == "Flee", "an enum exports its member name")
    check(payload["Nickname"] is None, "an empty string with no declared default exports null")
    check(payload["Home"] == [1.0, 2.0, 3.0], "a vector exports as a flat float array")
    check(payload["Tint"] == {"r": 1.0, "g": 0.5, "b": 0.0, "a": 1.0}, "a color exports as rgba")
    check(payload["Box"] == {"SizeX": 4.0, "SizeY": 2.0}, "a composed group re-nests from its paths")
    check("Shape" not in payload, "a host-baked field is absent, not guessed at")

    check(
        "Custom" not in entities["Plain"]["Components"],
        "an entity with nothing authored has no Custom key at all",
    )

    # -- entity-owned lights --------------------------------------------------------------
    owned = entities["OwnedSun"]["Components"].get("Light")
    check(owned is not None and owned["Type"] == "Directional", "a lamp entity owns its light")
    check("Light" not in entities["Creature"]["Components"], "non-lamp entities have no Light key")
    path = os.path.join(DATA_DIR, "scenes", "authored_test.json")
    with open(path, encoding="utf-8") as file:
        whole = json.load(file)
    state_lights = [light["Id"] for light in whole["Lighting"]["States"][0]["Lights"]]
    check(
        state_lights == ["SceneFill"],
        "an entity-owned lamp leaves the scene-level light list; an unowned one stays",
        f"state lights: {state_lights}",
    )

    # -- duplicate carries the values (same mechanism as the GUID gotcha) ----------------
    bpy.ops.object.select_all(action="DESELECT")
    creature_obj.select_set(True)
    bpy.context.view_layer.objects.active = creature_obj
    bpy.ops.object.duplicate()
    duplicate = bpy.context.active_object
    check(
        authored.enabled_component_ids(duplicate) == ["game.creature"],
        "a duplicated object carries its authored components",
    )
    bpy.data.objects.remove(duplicate, do_unlink=True)

    # -- host-list components: colliders live in the same panel now ----------------------
    merged = authored.schema_for_data_dir(DATA_DIR)
    collider_component = authored.component_by_id(merged, "paradise.collider")
    check(authored.is_authorable(collider_component), "the collider component is offered")
    check(
        not authored.is_present(creature_obj, collider_component),
        "absent with no marker and an empty list",
    )

    authored.enable_component(creature_obj, collider_component)
    check(
        authored.is_present(creature_obj, collider_component)
        and not [k for k in creature_obj.keys() if "paradise.collider/" in k],  # noqa: SIM118
        "adding it sets the marker and creates NO form fields",
    )
    export_scene(bpy.context.scene)
    check(
        exported_entities()["Creature"]["Components"]["Collider"] is None,
        "the marker alone exports nothing — the references are the data",
    )

    bpy.ops.object.empty_add(type="CUBE", location=(0, 0, 1))
    shape = bpy.context.active_object
    shape.name = "CreatureCollider"
    shape.paradise_collider.is_collider = True
    shape.paradise_collider.shape = "Box"
    shape.paradise_collider.size_source = "EXPLICIT"
    shape.paradise_collider.size = (1.0, 1.0, 1.0)
    shape.parent = creature_obj
    creature_obj.paradise.physics_colliders.add().target = shape
    export_scene(bpy.context.scene)
    components = exported_entities()["Creature"]["Components"]
    check(
        components["Collider"] is not None and len(components["Collider"]["Colliders"]) == 1,
        "an assigned reference exports the collider",
    )
    check(
        components["Rigidbody"] is not None and components["Rigidbody"]["BodyType"] == "Static",
        "the derived static body still rides along",
    )

    authored.disable_component(creature_obj, "paradise.collider")
    check(
        len(creature_obj.paradise.physics_colliders) == 0,
        "removing the component clears the references too",
    )
    bpy.data.objects.remove(shape, do_unlink=True)

    check(
        not authored.is_authorable(authored.component_by_id(merged, "paradise.renderable")),
        "derived components stay read-only rows, never addable",
    )

    # -- hot reload ---------------------------------------------------------------------
    grown = json.loads(json.dumps(SCHEMA))
    grown["components"][0]["fields"].append({"name": "Grumpy", "type": "bool", "default": False})
    del grown["components"][1]  # game.marker is gone: the game renamed or removed it
    write_schema(grown)

    reloaded = authored.schema_for_data_dir(DATA_DIR)
    check(
        authored.component_by_id(reloaded, "game.marker") is None
        and any(
            field.path == "Grumpy"
            for field in contract_authoring.flatten(
                authored.component_by_id(reloaded, "game.creature")
            )[0]
        ),
        "the schema hot-reloads when the file changes",
    )

    # -- a stale component is dropped from the export, not exported blind ----------------
    marker = authored.component_by_id(document, "game.marker")  # from the OLD schema
    authored.enable_component(creature_obj, marker)
    export_scene(bpy.context.scene)
    entities = exported_entities()
    ids = [entry["Id"] for entry in entities["Creature"]["Components"]["Custom"]]
    check(
        ids == ["game.creature"],
        "a component the schema no longer declares is not exported",
        f"exported ids: {ids}",
    )
    check(
        entities["Creature"]["Components"]["Custom"][0]["Data"]["Grumpy"] is False,
        "a field added by the new schema exports its default",
    )

    # -- removal cleans up --------------------------------------------------------------
    authored.disable_component(creature_obj, "game.creature")
    authored.disable_component(creature_obj, "game.marker")
    leftovers = [
        key
        for key in creature_obj.keys()  # noqa: SIM118 -- bpy Object is not a dict
        if key.startswith(authored.VALUE_PREFIX)
    ]
    check(leftovers == [], "removing a component deletes its ID properties", str(leftovers))
    check(
        authored.ENABLED_KEY not in creature_obj.keys(),  # noqa: SIM118 -- bpy, not a dict
        "removing the last component removes the marker too",
    )

    # Leave the schema-driven export in place (re-export after removal would drop Custom from
    # the document the conformance gate checks). Re-enable and export once more.
    authored.enable_component(creature_obj, authored.component_by_id(reloaded, "game.creature"))
    export_scene(bpy.context.scene)

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(f"All checks passed. Document in {DATA_DIR}/scenes/authored_test.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
