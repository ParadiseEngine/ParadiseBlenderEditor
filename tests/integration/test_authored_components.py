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
import math
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

import bpy  # noqa: E402

import paradise_blender  # noqa: E402
from paradise_blender.authoring import authored_components as authored  # noqa: E402
from paradise_blender.contract import authoring as contract_authoring  # noqa: E402
from paradise_blender.contract import component_ids  # noqa: E402
from paradise_blender.export.scene import export_scene  # noqa: E402


def payload_for(entity, component_id):
    """One component's Data on an exported entity, or None. Components are a LIST now, so a test
    asks by id rather than by a key whose absence used to mean "no such component"."""
    for component in entity["Components"]:
        if component["Id"] == component_id:
            return component["Data"]
    return None


DATA_DIR = os.path.join(tempfile.gettempdir(), "paradise_export_test")

failures: list[str] = []


def check(condition: bool, description: str, detail: str = "") -> None:
    if condition:
        print(f"ok   {description}")
    else:
        print(f"FAIL {description}{(' — ' + detail) if detail else ''}")
        failures.append(description)


#: A game's components carry GUIDs like everything else since v3; these two are fixed values so
#: the assertions below can name them.
CREATURE_ID = "c4e8a1b2-9f60-4d33-8a17-6b2e50d9fc84"
MARKER_ID = "2d7f36ae-51c8-4b90-8e42-9a0b7cd1e5f3"
DOORWAY_ID = "7a1c9e04-3b52-4f18-9d6a-c084e2571b93"
GAME_IDS = {CREATURE_ID, MARKER_ID, DOORWAY_ID}

# A LAUNCHER's dump, which is the only kind of authoring schema this host reads now: the game's
# own components AND the engine's, in one document. It used to be a game-only fixture, with the
# engine's half supplied by a vendored copy this addon merged underneath — that copy is gone,
# because a launcher built with ParadiseAuthoringScanReferences merges every assembly it
# references into what it dumps. The engine entries below are transcribed from that dump; only
# the ones these checks actually reason about are present.
SCHEMA = {
    "version": 3,
    "components": [
        {
            "id": component_ids.RIGIDBODY,
            "type": "Paradise.Export.Data.RigidbodyComponentData",
            "displayName": "Rigidbody",
            "fields": [{"name": "Mass", "type": "float", "default": 0}],
        },
        {
            "id": component_ids.AGENT,
            "type": "Paradise.Export.Data.AgentComponentData",
            "displayName": "Agent (movement)",
            "fields": [{"name": "MoveSpeed", "type": "float", "default": 1}],
        },
        {
            "id": component_ids.RENDERABLE,
            "type": "Paradise.Export.Data.RenderableComponentData",
            "displayName": "Renderable",
            "authoredBy": "mesh",
            "fields": [{"name": "Mesh", "type": "string"}],
        },
        {
            "id": component_ids.LIGHT,
            "type": "Paradise.Export.Data.SceneLightData",
            "displayName": "Light",
            "authoredBy": "light",
            "fields": [{"name": "Energy", "type": "float", "default": 1}],
        },
        {
            "id": CREATURE_ID,
            "type": "Game.Creature",
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
            "id": MARKER_ID,
            "type": "Game.Marker",
            "displayName": "Marker",
            "fields": [{"name": "Label", "type": "string", "default": "spawn"}],
        },
        {
            # The one authoredBy kind this host authors: an object slot whose world pose is baked.
            # Declares only PART of the pose, which is the interesting case — an exporter fills
            # what the record asked for and invents nothing.
            "id": DOORWAY_ID,
            "type": "Game.Doorway",
            "displayName": "Doorway",
            "fields": [
                {
                    "name": "Destination",
                    "type": "object",
                    "authoredBy": "transform",
                    # All four pose leaves the exporter knows how to bake. ShiningPie's real record
                    # takes Position/Rotation/Scale and derives its heading at load; Yaw is here so
                    # the derived leaf is covered too. (The "a declared leaf that is not a pose
                    # field is left alone" case is a unit test — it needs no Blender.)
                    "fields": [
                        {"name": "Position", "type": "vector3"},
                        {"name": "Rotation", "type": "quaternion"},
                        {"name": "Scale", "type": "vector3"},
                        {"name": "Yaw", "type": "float", "unit": "radians"},
                    ],
                },
            ],
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

    # A reference TARGET, deliberately not an entity: what a pose reference points at is a place,
    # not something the document has to contain. Blender (3, -5, 2) is contract (3, 2, 5) — the
    # (x, y, z) -> (x, z, -y) rebase — and a Z rotation survives as the contract yaw unchanged.
    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(3, -5, 2))
    target = bpy.context.active_object
    target.name = "DoorTarget"
    target.rotation_euler = (0.0, 0.0, math.pi / 4)
    # NON-UNIFORM on purpose: the basis change PERMUTES the axes, so a Blender scale of (2, 3, 4)
    # is a contract scale of (2, 4, 3). A converted-then-decomposed matrix gets that right by
    # construction and a per-component conversion does not — the trap CONVENTIONS.md opens with.
    target.scale = (2.0, 3.0, 4.0)


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
    game_ids = {c.id for c in document.components if c.id in GAME_IDS}
    check(game_ids == GAME_IDS, "every game component is read")
    check(
        authored.component_by_id(document, component_ids.RIGIDBODY) is not None,
        "the launcher's dump carries the engine's components beside the game's",
    )
    check(
        not authored.is_authorable(authored.component_by_id(document, component_ids.RENDERABLE))
        and not authored.is_authorable(authored.component_by_id(document, component_ids.LIGHT))
        and authored.is_authorable(authored.component_by_id(document, component_ids.AGENT)),
        "host-owned and host-baked engine components are not offered; plain ones are",
    )

    # -- storage ------------------------------------------------------------------------
    component = authored.component_by_id(document, CREATURE_ID)
    authored.enable_component(creature_obj, component)
    check(
        authored.enabled_component_ids(creature_obj) == [CREATURE_ID],
        "enabling records the component id",
    )

    friendly_key = authored.value_key(CREATURE_ID, "Friendly")
    check(
        isinstance(creature_obj[friendly_key], bool),
        "a bool field is stored as a real bool ID property",
        f"stored type: {type(creature_obj[friendly_key]).__name__}",
    )
    check(
        creature_obj[authored.value_key(CREATURE_ID, "Mode")] == "Chase",
        "an enum field starts on its declared default member",
    )

    creature_obj[authored.value_key(CREATURE_ID, "MaxSpeed")] = 9.25
    creature_obj[authored.value_key(CREATURE_ID, "Mode")] = "Flee"
    creature_obj[authored.value_key(CREATURE_ID, "Friendly")] = False
    creature_obj[authored.value_key(CREATURE_ID, "Box/SizeX")] = 4.0

    # -- export -------------------------------------------------------------------------
    export_scene(bpy.context.scene)
    entities = exported_entities()

    game_entries = [e for e in entities["Creature"]["Components"] if e["Id"] in GAME_IDS]
    check(len(game_entries) == 1, "the authored component is exported")
    payload = game_entries[0]["Data"]
    check(game_entries[0]["Id"] == CREATURE_ID, "under its schema id")
    check(game_entries[0]["Type"] == "Game.Creature", "carrying the CLR name beside the id")
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
        not [e for e in entities["Plain"]["Components"] if e["Id"] in GAME_IDS],
        "an entity with nothing authored contributes no game components",
    )

    # -- pose references: the one authoredBy kind this host authors ----------------------
    #
    # Authored as a REFERENCE, exported as a VALUE. Everything below is about that asymmetry:
    # what is stored is an object NAME, what travels is where that object stands, and the
    # conversion into the contract's Y-up basis is the same one every other transform goes
    # through. None of it is visible to a unit test, which has no bpy and therefore no objects.
    plain_obj = bpy.data.objects["Plain"]
    doorway = authored.component_by_id(authored.schema_for_data_dir(DATA_DIR), DOORWAY_ID)
    authored.enable_component(plain_obj, doorway)
    reference_key = authored.value_key(DOORWAY_ID, "Destination")
    check(
        reference_key in plain_obj.keys(),  # noqa: SIM118 -- bpy Object is not a dict
        "enabling a component creates a key for its object slot",
    )
    check(plain_obj[reference_key] == "", "an object slot starts unassigned")
    # The panel binds its picker with prop_search(obj, '["<key>"]', bpy.data, "objects"), and that
    # RNA path is the half that can be wrong without anyone noticing: the key carries a ':' and a
    # '/' inside the brackets, and a background run draws no panel to catch it. Resolving the path
    # is not the widget, but it IS the binding — a path that does not resolve cannot draw.
    try:
        resolved = plain_obj.path_resolve(f'["{reference_key}"]')
        check(resolved == "", "the picker's RNA path resolves to the stored name", repr(resolved))
    # Broad on purpose: the check IS whether path_resolve raises, so narrowing it would let
    # the failure mode this exists to catch escape as a traceback instead of a failed check.
    except Exception as error:
        check(False, "the picker's RNA path resolves to the stored name", str(error))

    export_scene(bpy.context.scene)
    payload = payload_for(exported_entities()["Plain"], DOORWAY_ID)
    check(
        payload is not None and payload["Destination"]["Position"] == [0.0, 0.0, 0.0]
        and payload["Destination"]["Scale"] == [0.0, 0.0, 0.0],
        "an unassigned slot exports every leaf at its schema default, not a guessed pose",
        str(payload),
    )

    plain_obj[reference_key] = "DoorTarget"
    export_scene(bpy.context.scene)
    payload = payload_for(exported_entities()["Plain"], DOORWAY_ID)
    check(
        payload["Destination"]["Position"] == [3.0, 2.0, 5.0],
        "an assigned slot bakes the target's world position, rebased Z-up -> Y-up",
        str(payload),
    )
    check(
        abs(payload["Destination"]["Yaw"] - math.pi / 4) < 1e-5,
        "and its yaw, as the atan2 the runtime reads a heading with",
        str(payload),
    )
    # A Blender Z rotation of pi/4 is a contract rotation about +Y of the same angle: the basis
    # change moves the axis and leaves the angle alone. Both halves are asserted because a
    # quaternion converted with the wrong component ORDER (Blender is wxyz, the contract xyzw)
    # produces a rotation that looks plausible and is wrong.
    rotation = payload["Destination"]["Rotation"]
    check(
        abs(rotation[1] - math.sin(math.pi / 8)) < 1e-5
        and abs(rotation[3] - math.cos(math.pi / 8)) < 1e-5
        and abs(rotation[0]) < 1e-5 and abs(rotation[2]) < 1e-5,
        "a rotation bakes about the contract's +Y, in xyzw order",
        str(rotation),
    )
    check(
        [round(v, 5) for v in payload["Destination"]["Scale"]] == [2.0, 4.0, 3.0],
        "a non-uniform scale is PERMUTED by the basis change, not copied component-wise",
        str(payload["Destination"]["Scale"]),
    )
    # The state the runtime tells "unassigned" apart by. An assigned slot always bakes a real
    # scale; only an unbaked one is all zeros, which is what makes it a reliable sentinel where
    # position (the world origin is a real place) is not.
    check(
        payload["Destination"]["Scale"] != [0.0, 0.0, 0.0],
        "and an assigned slot therefore never exports the unassigned sentinel",
    )

    # A reference is baked at EXPORT, so moving the target is the whole edit — nothing in the
    # panel mirrors the pose, and there is nothing to keep in sync.
    bpy.data.objects["DoorTarget"].location = (1, -1, 0)
    # matrix_world is evaluated, not stored: without this the bake reads the pose the object had
    # before the move and the check passes on stale data. Blender's own UI does this between
    # edits, which is why it is invisible until a script moves something and exports immediately.
    bpy.context.view_layer.update()
    export_scene(bpy.context.scene)
    payload = payload_for(exported_entities()["Plain"], DOORWAY_ID)
    check(
        payload["Destination"]["Position"] == [1.0, 0.0, 1.0],
        "moving the target is the edit — the pose is read at export, never mirrored",
        str(payload),
    )

    # A renamed or deleted target must NOT export as the origin: the origin is a real place, and
    # a silently-zeroed destination is indistinguishable from one somebody meant.
    plain_obj[reference_key] = "NoSuchObject"
    export_scene(bpy.context.scene)
    payload = payload_for(exported_entities()["Plain"], DOORWAY_ID)
    check(
        payload["Destination"]["Position"] == [0.0, 0.0, 0.0],
        "a dangling reference exports unauthored (and warns) rather than a made-up pose",
        str(payload),
    )

    plain_obj[reference_key] = "Plain"
    export_scene(bpy.context.scene)
    payload = payload_for(exported_entities()["Plain"], DOORWAY_ID)
    check(
        payload["Destination"]["Position"] == [0.0, 0.0, 0.0],
        "a self-reference carries no information and is refused the same way",
        str(payload),
    )

    authored.disable_component(plain_obj, DOORWAY_ID)
    check(
        reference_key not in plain_obj.keys(),  # noqa: SIM118 -- bpy Object is not a dict
        "removing the component removes its object slot too",
    )

    # -- entity-owned lights --------------------------------------------------------------
    owned = payload_for(entities["OwnedSun"], component_ids.LIGHT)
    check(owned is not None and owned["Type"] == "Directional", "a lamp entity owns its light")
    check(payload_for(entities["Creature"], component_ids.LIGHT) is None,
          "non-lamp entities author no light")
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
        authored.enabled_component_ids(duplicate) == [CREATURE_ID],
        "a duplicated object carries its authored components",
    )
    bpy.data.objects.remove(duplicate, do_unlink=True)

    # -- host-list components: colliders live in the same panel now ----------------------
    merged = authored.schema_for_data_dir(DATA_DIR)
    collider_component = authored.component_by_id(merged, component_ids.COLLIDER)
    check(authored.is_authorable(collider_component), "the collider component is offered")
    check(
        not authored.is_present(creature_obj, collider_component),
        "absent with no marker and an empty list",
    )

    authored.enable_component(creature_obj, collider_component)
    check(
        authored.is_present(creature_obj, collider_component)
        and not [k for k in creature_obj.keys() if authored.key_token(component_ids.COLLIDER) + "/" in k],  # noqa: SIM118
        "adding it sets the marker and creates NO form fields",
    )
    export_scene(bpy.context.scene)
    check(
        payload_for(exported_entities()["Creature"], component_ids.COLLIDER) is None,
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
    creature = exported_entities()["Creature"]
    collider = payload_for(creature, component_ids.COLLIDER)
    check(
        collider is not None and len(collider["Colliders"]) == 1,
        "an assigned reference exports the collider",
    )
    body = payload_for(creature, component_ids.RIGIDBODY)
    check(
        body is not None and body["BodyType"] == "Static",
        "the derived static body still rides along",
    )
    check(
        len([e for e in creature["Components"] if e["Id"] == component_ids.RIGIDBODY]) == 1,
        "and exactly once — a list does not enforce at-most-one the way a slot did",
    )

    authored.disable_component(creature_obj, component_ids.COLLIDER)
    check(
        len(creature_obj.paradise.physics_colliders) == 0,
        "removing the component clears the references too",
    )
    bpy.data.objects.remove(shape, do_unlink=True)

    check(
        not authored.is_authorable(authored.component_by_id(merged, component_ids.RENDERABLE)),
        "derived components stay read-only rows, never addable",
    )

    # -- a host-derived engine component is filtered before the exporter sees it -----------
    #
    # A .blend authored against an older schema can carry an id the host now derives. It must be
    # dropped with a warning rather than exported as if it were a game component.
    derived_obj = bpy.data.objects.new("DerivedCarrier", None)
    bpy.context.scene.collection.objects.link(derived_obj)
    derived_obj.paradise.is_entity = True
    derived_obj[authored.ENABLED_KEY] = [component_ids.RENDERABLE]

    payloads = authored.build_component_payloads(derived_obj, DATA_DIR)
    check(
        payloads == [],
        "a host-derived engine component is refused, never exported as a game component",
        str(payloads),
    )
    bpy.data.objects.remove(derived_obj, do_unlink=True)

    # -- hot reload ---------------------------------------------------------------------
    grown = json.loads(json.dumps(SCHEMA))
    grown["components"][0]["fields"].append({"name": "Grumpy", "type": "bool", "default": False})
    del grown["components"][1]  # game.marker is gone: the game renamed or removed it
    write_schema(grown)

    reloaded = authored.schema_for_data_dir(DATA_DIR)
    check(
        authored.component_by_id(reloaded, MARKER_ID) is None
        and any(
            field.path == "Grumpy"
            for field in contract_authoring.flatten(
                authored.component_by_id(reloaded, CREATURE_ID)
            )[0]
        ),
        "the schema hot-reloads when the file changes",
    )

    # -- a stale component is dropped from the export, not exported blind ----------------
    marker = authored.component_by_id(document, MARKER_ID)  # from the OLD schema
    authored.enable_component(creature_obj, marker)
    export_scene(bpy.context.scene)
    entities = exported_entities()
    ids = [e["Id"] for e in entities["Creature"]["Components"] if e["Id"] in GAME_IDS]
    check(
        ids == [CREATURE_ID],
        "a component the schema no longer declares is not exported",
        f"exported ids: {ids}",
    )
    check(
        payload_for(entities["Creature"], CREATURE_ID)["Grumpy"] is False,
        "a field added by the new schema exports its default",
    )

    # -- removal cleans up --------------------------------------------------------------
    authored.disable_component(creature_obj, CREATURE_ID)
    authored.disable_component(creature_obj, MARKER_ID)
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
    authored.enable_component(creature_obj, authored.component_by_id(reloaded, CREATURE_ID))
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
