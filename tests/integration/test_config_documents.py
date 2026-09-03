"""The Config panel's document store, end to end inside Blender.

Covers what a unit test cannot see: that config values survive a trip through Blender's ID
property system with their schema types intact, that the operators the panel's buttons invoke
actually load and save, and -- the one that matters most -- that a save does not destroy the parts
of a config file no editor understands.

That last check is the reason this file exists. A game's config is hand-written and carries prose
keys and whole content sections alongside the authored payloads. The merge is unit-tested in
``tests/unit/test_config_document.py``; this proves the operator wired to the Save button really
goes through it.

Authored LISTS are covered here too, and they belong at this layer rather than in a unit test:
the rows live in Blender ID properties keyed by indexed path, and add/remove/reorder are key
renumbering operations whose failure mode is an orphaned key that no pure test can see.

Run with::

    blender --background --factory-startup --python tests/integration/test_config_documents.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

import bpy  # noqa: E402

import paradise_blender  # noqa: E402
from paradise_blender.authoring import config_store  # noqa: E402
from paradise_blender.contract import authoring as contract_authoring  # noqa: E402
from paradise_blender.contract import config_document  # noqa: E402

DATA_DIR = os.path.join(tempfile.gettempdir(), "paradise_config_test")
CONFIG_PATH = os.path.join(DATA_DIR, "game", "config.json")

failures: list[str] = []


def check(condition: bool, description: str, detail: str = "") -> None:
    if condition:
        print(f"ok   {description}")
    else:
        print(f"FAIL {description}{(' — ' + detail) if detail else ''}")
        failures.append(description)


PLAYER_ID = "a17c9d02-4e6b-4f31-9d58-3c0b7e2a6194"
CAMERA_ID = "6b3e5f81-0c94-4a27-b6de-72f1849cad05"
#: Declared by the config file but not by the schema — a group the game deleted.
GONE_ID = "f4a8b217-3e05-4c96-8b7d-1e6045d9a3c2"
#: An authored LIST of records, each holding its own list — the deepest shape the path grammar
#: carries, and the one ShiningPie's drop tables have.
LOOT_ID = "854a7056-8b18-4c66-b778-b974ab2d2f3e"

SCHEMA = {
    "version": 3,
    "components": [
        {
            "id": PLAYER_ID,
            "type": "Game.Tuning.PlayerConfig",
            "displayName": "Player tuning",
            "fields": [
                {"name": "MaxSpeed", "type": "float", "unit": "meters",
                 "doc": "Top walking speed.", "minimum": 0.1, "maximum": 50, "default": 6},
                {"name": "Lives", "type": "int", "default": 3},
                {"name": "Mode", "type": "enum", "values": ["Idle", "Chase"], "default": "Idle"},
            ],
        },
        {
            "id": LOOT_ID,
            "type": "Game.Tuning.LootTableConfig",
            "displayName": "Loot tables",
            "fields": [
                {
                    "name": "Tables",
                    "type": "array",
                    "items": {
                        "name": "",
                        "type": "object",
                        "fields": [
                            {"name": "Table", "type": "string", "default": ""},
                            {"name": "MaxItems", "type": "int", "default": 1},
                            {
                                "name": "Entries",
                                "type": "array",
                                "items": {
                                    "name": "",
                                    "type": "object",
                                    "fields": [
                                        {"name": "Item", "type": "string", "default": ""},
                                        {"name": "Weight", "type": "int", "default": 1},
                                    ],
                                },
                            },
                        ],
                    },
                }
            ],
        },
        {
            "id": CAMERA_ID,
            "type": "Game.Tuning.CameraConfig",
            "displayName": "Camera tuning",
            "fields": [
                {
                    "name": "OnFoot",
                    "type": "object",
                    "fields": [
                        {"name": "YawDegrees", "type": "float", "default": 130},
                        {"name": "Distance", "type": "float", "default": 11},
                    ],
                }
            ],
        },
    ],
}

# Deliberately shaped like a real hand-written config: prose keys, a content section the addon
# knows nothing about, and a value that differs from the schema default.
CONFIG = {
    "// note": "Every gameplay tunable lives here.",
    "Components": [
        {"Id": PLAYER_ID, "Data": {"MaxSpeed": 6.5, "Lives": 5, "Mode": "Chase"}},
        {"Id": CAMERA_ID, "Data": {"OnFoot": {"YawDegrees": 130.0, "Distance": 12.0}}},
        {
            "Id": LOOT_ID,
            "Data": {
                "Tables": [
                    {"Table": "Rubble", "MaxItems": 2,
                     "Entries": [{"Item": "metal", "Weight": 5}]},
                    {"Table": "Crate", "MaxItems": 1, "Entries": []},
                    {"Table": "Chest", "MaxItems": 4,
                     "Entries": [{"Item": "gold", "Weight": 1}, {"Item": "gem", "Weight": 9}]},
                ]
            },
        },
    ],
    # A section no editor understands, kept OUTSIDE Components. The drop tables used to be the
    # example here; they are an authored payload now, so this stands in for whatever a game still
    # keeps to itself. The property under test is unchanged: the addon rewrites payloads and
    # nothing else.
    "// Dialogue": "Localized barks; not an authored payload.",
    "Dialogue": [{"Line": "who goes there", "Speaker": "guard"}],
}


def write(path: str, document: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(document, file, indent=2)


def main() -> int:
    paradise_blender.register()

    # Start from an empty tree: discovery reports what is actually on disk, so a file left by a
    # previous run would change what this test sees.
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    write(contract_authoring.schema_path(DATA_DIR), SCHEMA)
    write(CONFIG_PATH, CONFIG)

    scene = bpy.context.scene
    scene.paradise_project.data_dir = DATA_DIR

    # ---- the configurable list ------------------------------------------------------------
    # The file is picked from what discovery found under data/, not typed.
    discovered = config_document.discover(DATA_DIR)
    check(discovered == ["game/config.json"], "discovery finds the config document", str(discovered))

    check(
        bpy.ops.paradise.pick_config_document(index=-1, file="game/config.json") == {"FINISHED"},
        "pick operator reports FINISHED",
    )
    entry = config_store.active_document(scene)
    check(entry is not None, "the picked row is the active document")
    check(entry.file == "game/config.json", "the row stores a data-relative file")

    # ---- load -----------------------------------------------------------------------------
    check(bpy.ops.paradise.load_config_document() == {"FINISHED"}, "load operator reports FINISHED")

    key = config_store.config_value_key
    prefix = config_store.prefix_for(entry)
    check(
        abs(scene[key(prefix, PLAYER_ID, "MaxSpeed")] - 6.5) < 1e-6,
        "the file's value wins over the schema default",
        str(scene.get(key(prefix, PLAYER_ID, "MaxSpeed"))),
    )
    check(scene[key(prefix, PLAYER_ID, "Lives")] == 5, "int keeps its type")
    check(scene[key(prefix, PLAYER_ID, "Mode")] == "Chase", "enum loads by name")
    check(
        abs(scene[key(prefix, CAMERA_ID, "OnFoot/Distance")] - 12.0) < 1e-6,
        "a nested group loads by slash path",
    )
    check(
        config_store.loaded_stamp(scene, prefix) == config_document.config_stamp(CONFIG_PATH),
        "the loaded stamp matches the file",
    )

    ui = scene.id_properties_ui(key(prefix, PLAYER_ID, "MaxSpeed"))
    check(ui.as_dict().get("description") == "Top walking speed.", "schema doc reaches the tooltip")
    check(ui.as_dict().get("max") == 50, "schema range reaches the slider")
    subtype = ui.as_dict().get("subtype")
    check(
        subtype not in (None, "", "NONE", "none"),
        "meters become a distance spinner",
        repr(subtype),
    )

    # ---- edit and save --------------------------------------------------------------------
    scene[key(prefix, PLAYER_ID, "MaxSpeed")] = 9.25
    scene[key(prefix, CAMERA_ID, "OnFoot/Distance")] = 20.0
    check(bpy.ops.paradise.save_config_document() == {"FINISHED"}, "save operator reports FINISHED")

    with open(CONFIG_PATH, encoding="utf-8") as file:
        saved = config_document.read(file.read())

    check(
        config_document.payload_of(saved, PLAYER_ID)["MaxSpeed"] == 9.25,
        "the edited value reached the file",
    )
    check(
        config_document.payload_of(saved, CAMERA_ID)["OnFoot"]["Distance"] == 20.0,
        "a nested group re-nests on save",
    )
    check(
        config_document.payload_of(saved, PLAYER_ID)["Mode"] == "Chase",
        "an untouched field is written, not dropped",
    )

    # The whole point: everything the editor does not understand is still there.
    check(saved.get("// note") == CONFIG["// note"], "prose keys survive a save")
    check(saved.get("// Dialogue") == CONFIG["// Dialogue"], "section prose survives a save")
    check(saved.get("Dialogue") == CONFIG["Dialogue"], "content sections survive a save")
    check(
        list(saved) == ["// note", "Components", "// Dialogue", "Dialogue"],
        "top-level key order survives a save",
        str(list(saved)),
    )

    # ---- a group the schema no longer declares --------------------------------------------
    stale = dict(CONFIG)
    stale["Components"] = [*CONFIG["Components"], {"Id": GONE_ID, "Data": {"X": 1}}]
    write(CONFIG_PATH, stale)
    bpy.ops.paradise.load_config_document()
    bpy.ops.paradise.save_config_document()
    with open(CONFIG_PATH, encoding="utf-8") as file:
        after = config_document.read(file.read())
    check(
        config_document.payload_of(after, GONE_ID) == {"X": 1},
        "a group the schema dropped is left untouched rather than rewritten",
    )

    # ---- a second document in the same list -------------------------------------------------
    # ---- authored lists ---------------------------------------------------------------------
    # The store keeps one flat key per leaf at an indexed path, plus a "#" count per list. These
    # checks are at the KEY level as well as through the file: a renumbering bug leaves an orphan
    # that still round-trips correctly today and resurrects a deleted row on the next load.
    def loot_counts():
        return config_store.counts_for_store(scene, prefix, LOOT_ID)

    def loot_keys():
        head = config_store.component_key_prefix(prefix, LOOT_ID)
        # scene.keys() is the ID-property listing, not a dict view -- `in scene` asks a
        # different question, so the explicit call is correct here.
        return {k[len(head):] for k in scene.keys() if k.startswith(head)}  # noqa: SIM118

    def predicted_keys():
        plan = contract_authoring.outline(loot_component(), loot_counts())
        return ({f.path for f in plan.fields}
                | {a.path + contract_authoring.COUNT_SUFFIX for a in plan.arrays})

    def loot_component():
        from paradise_blender.authoring import authored_components as authored
        schema = authored.schema_for_data_dir(DATA_DIR)
        return authored.component_by_id(schema, LOOT_ID)

    def saved_tables():
        with open(CONFIG_PATH, encoding="utf-8") as file:
            document = json.load(file)
        return config_document.payload_of(document, LOOT_ID)["Tables"]

    check(loot_counts().get("Tables") == 3, "a list loads its row count")
    check(loot_counts().get("Tables/1/Entries") == 0,
          "row counts are per instance, not per declaration")
    check(scene.get(key(prefix, LOOT_ID, "Tables/2/Entries/1/Item")) == "gem",
          "a nested row's leaf loads at its indexed path")
    check(loot_keys() == predicted_keys(), "the store holds exactly the keys the outline predicts")

    # A no-op save must not disturb a hand-written file, key order included: an authored list is
    # written interleaved with its siblings rather than hoisted above them.
    with open(CONFIG_PATH, encoding="utf-8") as file:
        untouched = file.read()
    bpy.ops.paradise.save_config_document()
    with open(CONFIG_PATH, encoding="utf-8") as file:
        check(file.read() == untouched, "a save with no edits leaves the file byte-identical")

    # ---- add a row ----------------------------------------------------------------------------
    check(
        bpy.ops.paradise.config_row_add(prefix=prefix, component=LOOT_ID, path="Tables")
        == {"FINISHED"},
        "add row reports FINISHED",
    )
    check(loot_counts().get("Tables") == 4, "adding a row raises the count")
    check(loot_counts().get("Tables/3/Entries") == 0,
          "a new row's own nested list starts empty and says so")
    bpy.ops.paradise.save_config_document()
    check(len(saved_tables()) == 4 and saved_tables()[3] == {"Table": "", "MaxItems": 1,
                                                             "Entries": []},
          "the added row reaches the file as schema defaults", str(saved_tables()[-1]))

    # ---- remove a row -------------------------------------------------------------------------
    # Row 1 of 4: rows 2 and 3 shift down, and row 2's ENTRIES have to travel with it. This is the
    # case that catches a renumbering that moves leaves but forgets nested counts.
    check(
        bpy.ops.paradise.config_row_remove(prefix=prefix, component=LOOT_ID, path="Tables", index=1)
        == {"FINISHED"},
        "remove row reports FINISHED",
    )
    check(loot_counts().get("Tables") == 3, "removing a row lowers the count")
    check(scene.get(key(prefix, LOOT_ID, "Tables/1/Table")) == "Chest",
          "the row above the removed one shifts down")
    check(loot_counts().get("Tables/1/Entries") == 2,
          "a shifted row's nested COUNT travels with it")
    check(scene.get(key(prefix, LOOT_ID, "Tables/1/Entries/1/Item")) == "gem",
          "a shifted row's nested VALUES travel with it")
    check(loot_keys() == predicted_keys(), "removing a row leaves no orphaned keys")

    # ---- move a row ---------------------------------------------------------------------------
    check(
        bpy.ops.paradise.config_row_move(
            prefix=prefix, component=LOOT_ID, path="Tables", index=0, direction="DOWN")
        == {"FINISHED"},
        "move row reports FINISHED",
    )
    check(scene.get(key(prefix, LOOT_ID, "Tables/0/Table")) == "Chest",
          "moving down swaps the two rows")
    check(scene.get(key(prefix, LOOT_ID, "Tables/0/Entries/1/Item")) == "gem",
          "a moved row's entries follow it")
    check(loot_keys() == predicted_keys(), "moving a row leaves no orphaned keys")

    # id_properties_ui belongs to the KEY, so a moved row loses its tooltip and slider range
    # unless they are re-applied. Nothing fails when they are not -- it just quietly degrades.
    moved_ui = scene.id_properties_ui(key(prefix, LOOT_ID, "Tables/0/Entries/1/Weight")).as_dict()
    check(bool(moved_ui.get("description") is not None or moved_ui),
          "UI metadata survives a row move", str(moved_ui))

    bpy.ops.paradise.save_config_document()
    check([t["Table"] for t in saved_tables()] == ["Chest", "Rubble", ""],
          "the reordered rows reach the file in their new order",
          str([t["Table"] for t in saved_tables()]))
    check(saved_tables()[0]["Entries"] == [{"Item": "gold", "Weight": 1},
                                           {"Item": "gem", "Weight": 9}],
          "nested rows survive the whole trip", str(saved_tables()[0]["Entries"]))

    # ---- an empty list is authored, not absent ------------------------------------------------
    for _ in range(len(saved_tables())):
        # Always index 0: each removal shifts the rest down, so this drains the list.
        bpy.ops.paradise.config_row_remove(
            prefix=prefix, component=LOOT_ID, path="Tables", index=0)
    bpy.ops.paradise.save_config_document()
    check(saved_tables() == [], "a list emptied by hand is written as [] rather than dropped")

    # ---- reload drops the tail ----------------------------------------------------------------
    # Rows are data, not schema: a reload of a file with fewer rows than the store holds must not
    # leave the old tail behind for the next save to resurrect.
    bpy.ops.paradise.load_config_document()
    check(loot_counts().get("Tables") == 0, "reloading adopts the file's row count")
    check(loot_keys() == predicted_keys(), "reloading leaves no keys from the previous load")

    # Two rows must not share a namespace: the values of one are not the values of the other.
    second_path = os.path.join(DATA_DIR, "game", "other.json")
    write(second_path, {"Components": [{"Id": PLAYER_ID, "Data": {"MaxSpeed": 1.5}}]})
    before = scene[key(prefix, PLAYER_ID, "MaxSpeed")]
    bpy.ops.paradise.pick_config_document(index=-1, file="game/other.json")
    second = config_store.active_document(scene)
    check(second.file == "game/other.json", "the second row stores its own file")
    bpy.ops.paradise.load_config_document()
    second_prefix = config_store.prefix_for(second)
    check(second_prefix != prefix, "each document gets its own namespace")
    check(
        abs(scene[key(second_prefix, PLAYER_ID, "MaxSpeed")] - 1.5) < 1e-6,
        "the second document loads its own values",
    )
    check(
        abs(scene[key(prefix, PLAYER_ID, "MaxSpeed")] - before) < 1e-6,
        "the first document's values are untouched by the second",
        f"was {before}, now {scene[key(prefix, PLAYER_ID, 'MaxSpeed')]}",
    )

    # ---- removing a row forgets its values --------------------------------------------------
    check(bpy.ops.paradise.remove_config_document() == {"FINISHED"}, "remove reports FINISHED")
    check(
        key(second_prefix, PLAYER_ID, "MaxSpeed") not in scene,
        "removing a document drops its stored values",
    )
    check(os.path.exists(second_path), "removing a document leaves its FILE alone")

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(f"All checks passed. Config at {CONFIG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
