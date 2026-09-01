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
from paradise_blender.contract import component_ids, well_known  # noqa: E402
from paradise_blender.export.scene import export_scene  # noqa: E402


def payload_for(entity, component_id):
    """One component's Data on an exported entity, or None. Components are a LIST now, so a test
    asks by id rather than by a key whose absence used to mean "no such component"."""
    for component in entity:
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
#: A game component whose BODY is object references, exactly as the engine's collider is. Before
#: host storage became schema-driven this was detected and then had nowhere to put a pointer, so
#: it drew as "not authorable in Blender yet" and exported nothing.
BODY_ID = "7c4e0a19-3d55-4b8e-9a2c-6e8f1b4d0c23"

#: A game component referencing ONE shape, not a list -- the scalar case.
VOLUME_ID = "3f7f5b6c-0346-4de7-9bf6-0dd4e25ac74c"

#: A game component whose references ARE their values -- a mesh to export, another object to name,
#: a file on disk. The other family of host reference: where a pose or a shape fills the LEAVES a
#: record declares under it, these three write at the reference's own path.
LEAFY_ID = "07f29866-de34-49ef-a2a9-4e71b1d4e250"

#: Self / light / camera field-level host kinds, plus a type-level camera component.
HOSTED_ID = "a1b2c3d4-e5f6-4789-8abc-def012345678"
BY_CAMERA_ID = "b2c3d4e5-f6a7-4890-9bcd-ef0123456789"

GAME_IDS = {CREATURE_ID, MARKER_ID, DOORWAY_ID, BODY_ID, VOLUME_ID, LEAFY_ID}
ALL_GAME_IDS = GAME_IDS | {HOSTED_ID, BY_CAMERA_ID}

# A LAUNCHER's dump, which is the only kind of authoring schema this host reads now: the game's
# own components AND the engine's, in one document. It used to be a game-only fixture, with the
# engine's half supplied by a vendored copy this addon merged underneath — that copy is gone,
# because a launcher built with ParadiseAuthoringScanReferences merges every assembly it
# references into what it dumps. The engine entries below are transcribed from that dump; only
# the ones these checks actually reason about are present.
SCHEMA = {
    "version": 3,
    "components": [
        # The three every export writes for every object it emits. They are host-derived rather
        # than authored in the panel, but they still have to be NAMEABLE: the exporter reads each
        # engine component's CLR type name out of this document.
        {
            "id": component_ids.NAME,
            "type": "Paradise.Export.Data.NameComponentData",
            "displayName": "Name",
            "fields": [{"name": "Value", "type": "string", "default": ""}],
        },
        {
            "id": component_ids.TRANSFORM,
            "type": "Paradise.Export.Data.TransformComponentData",
            "displayName": "Transform",
            "fields": [{"name": "World", "type": "matrix4x4"}],
        },
        {
            "id": component_ids.ENVIRONMENT,
            "type": "Paradise.Export.Data.EnvironmentData",
            "displayName": "Environment",
            "fields": [{"name": "TonemapMode", "type": "string", "default": "Linear"}],
        },
        {
            "id": LEAFY_ID,
            "type": "Game.Leafy",
            "displayName": "Leafy",
            "fields": [
                {"name": "Mesh", "type": "string", "authoredBy": "mesh"},
                {"name": "Target", "type": "string", "authoredBy": "entity"},
            ],
        },
        {
            "id": HOSTED_ID,
            "type": "Game.Hosted",
            "displayName": "Hosted",
            "fields": [
                {"name": "Ident", "type": "string", "authoredBy": "id"},
                {"name": "Label", "type": "string", "authoredBy": "name"},
                {
                    "name": "Lamp",
                    "type": "object",
                    "authoredBy": "light",
                    "fields": [
                        {"name": "Type", "type": "enum", "values": ["Directional", "Point", "Spot"]},
                        {"name": "Intensity", "type": "float", "default": 1},
                    ],
                },
                {
                    "name": "Eye",
                    "type": "object",
                    "authoredBy": "camera",
                    "fields": [
                        {"name": "Fov", "type": "float", "default": 50},
                        {"name": "Projection", "type": "enum", "values": ["Perspective", "Orthographic"]},
                    ],
                },
            ],
        },
        {
            "id": BY_CAMERA_ID,
            "type": "Game.ByCamera",
            "displayName": "By camera",
            "authoredBy": "camera",
            "fields": [
                {"name": "Fov", "type": "float", "default": 50},
                {"name": "Near", "type": "float", "default": 0.1},
            ],
        },
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
            # The engine's own host list. Declared here because this fixture IS the whole document
            # now -- the vendored engine schema was removed on purpose ("ONE DOCUMENT, AND IT IS
            # THE GAME'S"), and the checks below ask for this component by id.
            "id": component_ids.COLLIDER,
            "type": "Paradise.Export.Data.ColliderComponentData",
            "displayName": "Collider",
            "fields": [
                {
                    "name": "Colliders",
                    "type": "array",
                    "items": {
                        "name": "Colliders",
                        "type": "object",
                        "authoredBy": "shape",
                        "fields": [
                            {"name": "IsTrigger", "type": "bool"},
                            {"name": "ShapeType", "type": "string"},
                            {"name": "Size", "type": "vector3"},
                        ],
                    },
                }
            ],
        },
        {
            "id": VOLUME_ID,
            "type": "ShiningPie.Authoring.InteractionTriggerMarker",
            "displayName": "Interaction trigger",
            "fields": [
                {"name": "Prompt", "type": "string"},
                {
                    "name": "Volume",
                    "type": "object",
                    "authoredBy": "shape",
                    "fields": [
                        {"name": "IsTrigger", "type": "bool"},
                        {"name": "ShapeType", "type": "string"},
                        {"name": "Radius", "type": "float"},
                    ],
                },
            ],
        },
        {
            "id": BODY_ID,
            "type": "ShiningPie.Authoring.ActorBody",
            "displayName": "Actor body",
            "fields": [
                {
                    "name": "Shapes",
                    "type": "array",
                    "items": {
                        "name": "Shapes",
                        "type": "object",
                        "authoredBy": "shape",
                        "fields": [
                            {"name": "IsTrigger", "type": "bool"},
                            {"name": "ShapeType", "type": "string"},
                            {"name": "Radius", "type": "float"},
                            {"name": "Height", "type": "float"},
                        ],
                    },
                }
            ],
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

    bpy.ops.object.camera_add(location=(0, -8, 2))
    camera = bpy.context.active_object
    camera.name = "ShotCamera"
    camera.data.angle = math.radians(40.0)


def exported_entities() -> dict[str, list]:
    """The exported objects by name, and an object IS its component list since schema v5.

    The name comes off the object's own Name component, because there is nowhere else for it to
    come from — which is exactly the property being relied on here."""
    path = os.path.join(DATA_DIR, "scenes", "authored_test.json")
    with open(path, encoding="utf-8") as file:
        document = json.load(file)

    objects = {}
    for entity in document["Entities"]:
        payload = payload_for(entity, well_known.META_ID)
        if payload is not None and payload.get(well_known.NAME):
            objects[payload[well_known.NAME]] = entity
    return objects


def main() -> int:
    paradise_blender.register()
    write_schema(SCHEMA)
    build_scene()

    creature_obj = bpy.data.objects["Creature"]
    document = authored.schema_for_data_dir(DATA_DIR)
    check(authored.schema_load_error(DATA_DIR) is None, "the schema file loads")
    game_ids = {c.id for c in document.components if c.id in ALL_GAME_IDS}
    check(game_ids == ALL_GAME_IDS, "every game component is read")
    check(
        authored.component_by_id(document, component_ids.RIGIDBODY) is not None,
        "the launcher's dump carries the engine's components beside the game's",
    )
    check(
        not authored.is_authorable(authored.component_by_id(document, component_ids.RENDERABLE))
        and not authored.is_authorable(authored.component_by_id(document, component_ids.LIGHT))
        and authored.is_authorable(authored.component_by_id(document, component_ids.AGENT))
        and authored.is_authorable(authored.component_by_id(document, BY_CAMERA_ID)),
        "host-owned engine components are not offered; game host-kind components are",
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

    game_entries = [e for e in entities["Creature"] if e["Id"] in GAME_IDS]
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
    # An UNASSIGNED object slot exports its leaves at the record's own defaults, and does not
    # vanish from the payload. That is build_payload's stated rule and the one the runtime depends
    # on: a field that is simply absent reads as "this host does not implement the kind", while a
    # field present and empty reads as "nobody picked an object" — and only the second is something
    # a loader can refuse by name. The trigger volumes rely on exactly this.
    check(payload["Shape"] == {"SizeX": 0.0},
          "an unassigned object slot exports its declared leaves at their defaults",
          str(payload.get("Shape")))

    check(
        "Plain" not in entities,
        "an entity with nothing authored is not exported at all",
        str(sorted(entities)),
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
    # The routing rule this used to check is GONE, and that is the point of checking it here.
    # A lamp used to be either an entry in a document-level lighting state or a component on an
    # entity that owned it, with a rule saying it must not be both or the runtime would light it
    # twice. Since schema v5 every lamp is an OBJECT carrying a Light — an owned one because the
    # entity walk writes it, an unowned one because the scene walk gives it an object of its own —
    # so the two cannot disagree and there is no list to leave.
    fill = payload_for(entities["SceneFill"], component_ids.LIGHT)
    check(fill is not None, "an unowned lamp is an object carrying a Light too",
          str(sorted(entities)))
    lit = [name for name, obj in entities.items()
           if payload_for(obj, component_ids.LIGHT) is not None]
    check(sorted(lit) == ["OwnedSun", "SceneFill"],
          "each lamp is described exactly once, by exactly one object", str(sorted(lit)))

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

    # -- a GAME component whose items are host-authored shapes ---------------------------
    #
    # The capability this file exists to pin: storage for object references follows from the
    # SCHEMA (an array whose items say authoredBy) rather than from a hand-written map of the two
    # ids the engine happened to ship. Nothing in the addon knows this component's name.
    merged = authored.schema_for_data_dir(DATA_DIR)
    body_component = authored.component_by_id(merged, BODY_ID)
    check(body_component is not None, "the game's host-list component is read from the schema")
    check(
        authored.is_host_list(body_component),
        "a game component whose items are host-authored is a host list",
    )
    check(
        authored.host_ref_key(body_component) == f"{BODY_ID}/Shapes",
        "its references are keyed by component id and field path",
    )

    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(4, 0, 0))
    actor = bpy.context.active_object
    actor.name = "Actor"
    actor.paradise.is_entity = True
    authored.enable_component(actor, body_component)

    bpy.ops.object.empty_add(type="SPHERE", location=(4, 0, 0.9))
    body_shape = bpy.context.active_object
    body_shape.name = "ActorCapsule"
    body_shape.paradise_collider.is_collider = True
    body_shape.paradise_collider.shape = "Capsule"
    body_shape.paradise_collider.size_source = "EXPLICIT"
    body_shape.paradise_collider.radius = 0.35
    body_shape.paradise_collider.height = 1.8
    body_shape.parent = actor

    check(
        not authored.host_entries(actor, body_component),
        "no references before any object is assigned",
    )

    # Through the OPERATOR, which is what the panel's + button calls -- so this covers the key
    # reaching the store, not just the store working when written by hand.
    bpy.context.view_layer.objects.active = actor
    body_shape.select_set(True)
    bpy.ops.paradise.assign_colliders(key=authored.host_ref_key(body_component))
    entries = authored.host_entries(actor, body_component)
    check(
        len(entries) == 1 and entries[0].target is body_shape,
        "the operator stores the reference under the component's own key",
    )
    check(
        all(item.key == authored.host_ref_key(body_component) for item in actor.paradise.host_refs),
        "and every row in the shared store says which field it fills",
    )

    export_scene(bpy.context.scene)
    body_payload = payload_for(exported_entities()["Actor"], BODY_ID)
    check(
        body_payload is not None and len(body_payload.get("Shapes", [])) == 1,
        "the reference exports as a baked shape under the field's own name",
        f"payload={body_payload}",
    )
    if body_payload and body_payload.get("Shapes"):
        shape_json = body_payload["Shapes"][0]
        check(
            shape_json["ShapeType"] == "Capsule"
            and abs(shape_json["Radius"] - 0.35) < 1e-5
            and abs(shape_json["Height"] - 1.8) < 1e-5,
            "and it is the capsule the author drew, baked to values",
            f"shape={shape_json}",
        )

    # -- a SCALAR shape reference: one object slot, baked like a transform's --------------
    volume_component = authored.component_by_id(merged, VOLUME_ID)
    hosts = contract_authoring.outline(volume_component).hosts
    volume_host = next(h for h in hosts if h.path == "Volume")
    check(
        volume_host.kind == "shape" and volume_host.is_authorable,
        "a scalar shape reference is authorable, not merely reported",
    )

    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(8, 0, 0))
    pad = bpy.context.active_object
    pad.name = "Pad"
    pad.paradise.is_entity = True
    authored.enable_component(pad, volume_component)
    pad[authored.value_key(VOLUME_ID, "Prompt")] = "Open the shutter"

    bpy.ops.object.empty_add(type="CUBE", location=(8, 0, 0.5))
    sensor = bpy.context.active_object
    sensor.name = "PadSensor"
    sensor.paradise_collider.is_collider = True
    sensor.paradise_collider.shape = "Sphere"
    sensor.paradise_collider.size_source = "EXPLICIT"
    sensor.paradise_collider.radius = 2.5
    sensor.paradise_collider.is_trigger = True
    sensor.parent = pad

    export_scene(bpy.context.scene)
    unassigned = payload_for(exported_entities()["Pad"], VOLUME_ID)
    check(
        unassigned is not None and unassigned["Volume"]["ShapeType"] != "Sphere",
        "an unassigned slot exports the record's defaults, not a guessed shape",
        f"payload={unassigned}",
    )

    # The picker stores the object's NAME, exactly as a transform reference does.
    pad[authored.value_key(VOLUME_ID, "Volume")] = "PadSensor"
    export_scene(bpy.context.scene)
    baked = payload_for(exported_entities()["Pad"], VOLUME_ID)
    check(
        baked is not None
        and baked["Volume"]["ShapeType"] == "Sphere"
        and abs(baked["Volume"]["Radius"] - 2.5) < 1e-5
        and baked["Volume"]["IsTrigger"] is True,
        "an assigned slot bakes the collider drawn on the object it points at",
        f"payload={baked}",
    )

    pad[authored.value_key(VOLUME_ID, "Volume")] = "NoSuchCollider"
    export_scene(bpy.context.scene)
    dangling = payload_for(exported_entities()["Pad"], VOLUME_ID)
    check(
        dangling is not None and dangling["Volume"]["ShapeType"] != "Sphere",
        "a dangling shape reference exports unauthored (and warns) rather than a stale shape",
    )

    # -- leaf references: the reference IS the value -------------------------------------
    #
    # The other family. A pose or a shape fills the LEAVES a record declares under the reference;
    # a mesh, an object name or an asset path is written at the reference's own path. Exercised
    # here because bake_leaf_refs needs a real scene: one resolves geometry through the export's
    # own MeshExporter, and one resolves an object by name.
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(6, 0, 0))
    leafy = bpy.context.active_object
    leafy.name = "Leafy"
    leafy.paradise.is_entity = True
    authored.enable_component(leafy, authored.component_by_id(document, LEAFY_ID))

    export_scene(bpy.context.scene)
    unset = payload_for(exported_entities()["Leafy"], LEAFY_ID)
    check(
        unset is not None and unset["Mesh"] is None and unset["Target"] is None,
        "an unassigned leaf reference exports empty rather than vanishing from the payload",
        f"payload={unset}",
    )

    # POINTING AT ITSELF, which is the normal case for a mesh and the one the pose bake refuses:
    # an object usually draws its own geometry, and the slot exists so that "this draws" is
    # something the scene SAYS rather than something the exporter infers from it having a mesh.
    leafy[authored.value_key(LEAFY_ID, "Mesh")] = "Leafy"
    leafy[authored.value_key(LEAFY_ID, "Target")] = "Creature"
    export_scene(bpy.context.scene)
    baked = payload_for(exported_entities()["Leafy"], LEAFY_ID)
    check(
        baked is not None and (baked["Mesh"] or "").endswith(".glb"),
        "a mesh reference bakes the data-relative GLB the referenced object exported to",
        f"payload={baked}",
    )
    check(
        baked is not None and baked["Target"] == bpy.data.objects["Creature"].paradise.entity_guid,
        "an entity reference bakes the target's GUID, which is what every object carries",
        f"payload={baked}",
    )

    leafy[authored.value_key(LEAFY_ID, "Target")] = "NoSuchObject"
    export_scene(bpy.context.scene)
    dangling_target = payload_for(exported_entities()["Leafy"], LEAFY_ID)
    check(
        dangling_target is not None and dangling_target["Target"] is None,
        "a dangling entity reference exports empty rather than the name nobody could resolve",
        f"payload={dangling_target}",
    )

    # -- self / light / camera -----------------------------------------------------------
    hosted = authored.component_by_id(document, HOSTED_ID)
    authored.enable_component(creature_obj, hosted)
    check(
        authored.value_key(HOSTED_ID, "Ident") not in creature_obj.keys(),  # noqa: SIM118
        "a self kind stores no picker key",
    )
    creature_obj[authored.value_key(HOSTED_ID, "Lamp")] = "OwnedSun"
    creature_obj[authored.value_key(HOSTED_ID, "Eye")] = "ShotCamera"
    authored.enable_component(creature_obj, authored.component_by_id(document, BY_CAMERA_ID))
    creature_obj[authored.value_key(BY_CAMERA_ID, contract_authoring.HOST_SOURCE_PATH)] = "ShotCamera"
    export_scene(bpy.context.scene)
    hosted_payload = payload_for(exported_entities()["Creature"], HOSTED_ID)
    check(
        hosted_payload is not None
        and hosted_payload["Ident"] == creature_obj.paradise.entity_guid
        and hosted_payload["Label"] == "Creature",
        "self kinds bake this object's identity and name",
        f"payload={hosted_payload}",
    )
    check(
        hosted_payload is not None and hosted_payload["Lamp"]["Type"] == "Directional",
        "a light reference bakes the lamp it points at",
        f"payload={hosted_payload}",
    )
    check(
        hosted_payload is not None and abs(hosted_payload["Eye"]["Fov"] - 40.0) < 0.01
        and hosted_payload["Eye"]["Projection"] == "Perspective",
        "a camera reference bakes the lens it points at",
        f"payload={hosted_payload}",
    )
    by_camera = payload_for(exported_entities()["Creature"], BY_CAMERA_ID)
    check(
        by_camera is not None and abs(by_camera["Fov"] - 40.0) < 0.01,
        "a type-level camera merges the baked lens onto the payload",
        f"payload={by_camera}",
    )

    # Back to the actor: an operator polls the ACTIVE object, and the scalar-shape block above
    # left a collider selected.
    bpy.context.view_layer.objects.active = actor
    bpy.ops.paradise.remove_collider(key=authored.host_ref_key(body_component), index=0)
    check(
        not authored.host_entries(actor, body_component),
        "removing takes the row the panel pointed at",
    )

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
    bpy.context.view_layer.objects.active = creature_obj
    shape.select_set(True)
    bpy.ops.paradise.assign_colliders(key=authored.host_ref_key(collider_component))
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
        len([e for e in creature if e["Id"] == component_ids.RIGIDBODY]) == 1,
        "and exactly once — a list does not enforce at-most-one the way a slot did",
    )

    authored.disable_component(creature_obj, component_ids.COLLIDER)
    check(
        not authored.host_entries(creature_obj, collider_component),
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
    # BY ID, never by position. This grew a field onto components[0] and deleted components[1]
    # back when those happened to be the game's two; every engine entry added to the fixture since
    # has shifted them, so the mutation landed on whatever was there — which is how it came to add
    # "Grumpy" to the rigidbody and assert it on the creature.
    grown = json.loads(json.dumps(SCHEMA))
    by_id = {component["id"]: component for component in grown["components"]}
    by_id[CREATURE_ID]["fields"].append({"name": "Grumpy", "type": "bool", "default": False})
    # game.marker is gone: the game renamed or removed it.
    grown["components"] = [c for c in grown["components"] if c["id"] != MARKER_ID]
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
    ids = [e["Id"] for e in entities["Creature"] if e["Id"] in GAME_IDS]
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
    authored.disable_component(creature_obj, HOSTED_ID)
    authored.disable_component(creature_obj, BY_CAMERA_ID)
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
