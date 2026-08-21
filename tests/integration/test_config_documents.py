"""The Config panel's document store, end to end inside Blender.

Covers what a unit test cannot see: that config values survive a trip through Blender's ID
property system with their schema types intact, that the operators the panel's buttons invoke
actually load and save, and -- the one that matters most -- that a save does not destroy the parts
of a config file no editor understands.

That last check is the reason this file exists. A game's config is hand-written and carries prose
keys and whole content sections (ShiningPie's ``LootTables``) alongside the authored payloads. The
merge is unit-tested in ``tests/unit/test_config_document.py``; this proves the operator wired to
the Save button really goes through it.

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
    ],
    "// LootTables": "Weighted drop tables; not an authored payload.",
    "LootTables": [{"Table": "Rubble", "Entries": [{"Item": "metal", "Weight": 5}]}],
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
    check(saved.get("// LootTables") == CONFIG["// LootTables"], "section prose survives a save")
    check(saved.get("LootTables") == CONFIG["LootTables"], "content sections survive a save")
    check(
        list(saved) == ["// note", "Components", "// LootTables", "LootTables"],
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
