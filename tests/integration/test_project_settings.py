"""Project TOML editing through real RNA slots, without an active prefab or object.

Copies game declarations and source documents before exercising them when
PARADISE_ASSETS_PROJECT points to a game checkout. Never edits that checkout.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_panel import draw

import paradise_assets
from paradise_assets import edits, field_widgets, project_settings
from paradise_assets.document import authoring_documents, canonical_toml, component_schema
from paradise_assets.document.project import ProjectLayout
from paradise_assets.materialize import store


def check(condition, label):
    assert condition, label
    print("PASS  " + label)


def refresh():
    layout = draw(project_settings.PARADISE_ASSETS_PT_project_settings, bpy.context)
    for ident in layout.operators:
        namespace, name = ident.split(".")
        getattr(getattr(bpy.ops, namespace), name).get_rna_type()
    return layout


def slot(path):
    return next(item for item in bpy.context.window_manager.paradise_document_slots if item.path == path)


def change(path, value):
    item = slot(path)
    setattr(item, item.rna, value)


def structure(action, path, **kwargs):
    result = bpy.ops.paradise_assets.edit_project_structure(action=action, field_name=path, **kwargs)
    check(result == {"FINISHED"}, f"{action} {path}")
    return refresh()


def make_project(root):
    layout = ProjectLayout(str(root))
    (root / "assets").mkdir()
    (root / ".editor").mkdir()
    (root / ".editor" / "authoring-schema.json").write_text(json.dumps({"version": 3, "components": []}))
    fields = [
        {
            "name": "Settings",
            "type": "object",
            "fields": [
                {"name": "Speed", "type": "float", "default": 4},
                {"name": "Enabled", "type": "bool", "default": True},
                {"name": "Mode", "type": "enum", "values": ["First", "Second"], "default": "Second"},
            ],
        },
        {
            "name": "Items",
            "type": "array",
            "items": {
                "type": "object",
                "fields": [
                    {"name": "Name", "type": "string"},
                    {"name": "Kind", "type": "enum", "values": ["Currency", "Quest Item"]},
                    {"name": "Weight", "type": "float", "default": 1},
                    {"name": "Limit", "type": "int", "optional": True, "default": 7},
                    {"name": "Icon", "type": "asset", "assetKinds": [".png"]},
                ],
            },
        },
    ]
    (root / ".editor" / "authoring-documents.json").write_text(
        json.dumps(
            {
                "version": 1,
                "documents": [
                    {
                        "id": "settings",
                        "displayName": "Test Settings",
                        "path": "settings.toml",
                        "fields": fields,
                    },
                ],
            }
        )
    )
    path = root / "assets" / "settings.toml"
    path.write_text(
        '# Retain this comment until an edit\nUnknown = "keep"\n[[Items]]\n'
        'Name = "coin"\nKind = "Currency"\nLimit = 3\n[[Items]]\n'
        'Name = "key"\nKind = "Quest Item"\n'
    )
    return layout, path


def synthetic(root):
    located, path = make_project(root)
    context = bpy.context
    context.view_layer.objects.active = None
    with patch.object(store, "project_of", return_value=located):
        check(
            project_settings.PARADISE_ASSETS_PT_project_settings.poll(context),
            "settings panel available without a document or active object",
        )
        check(
            bpy.ops.paradise_assets.open_project_settings(document="settings") == {"FINISHED"},
            "choose settings using its registered operator",
        )
        refresh()
        check(
            slot("Settings/Speed").value_float == 4
            and slot("Settings/Enabled").value_bool
            and slot("Settings/Mode").value_enum == "Second"
            and slot("Items/0/Weight").value_float == 1,
            "omitted values show schema defaults without creating edits",
        )
        check(edits.count(context.window_manager) == 0, "drawing is not an edit")
        original = path.read_bytes()
        bpy.ops.paradise_assets.save_project_settings()
        check(path.read_bytes() == original, "no-op settings save preserves original bytes and comments")

        change("Settings/Speed", 8.5)
        current = canonical_toml.loads(path.read_text())
        current["External"] = "new"
        path.write_text(canonical_toml.dumps(current))
        bpy.ops.paradise_assets.save_project_settings()
        saved = canonical_toml.loads(path.read_text())
        check(
            saved["Unknown"] == "keep" and saved["External"] == "new" and saved["Settings"] == {"Speed": 8.5},
            "nested edit preserves disk additions and omitted defaults",
        )

        refresh()
        structure("remove", "Items", index=0)
        change("Items/0/Name", "quest-key")
        layout = refresh()
        reverts = [
            props.field_name
            for ident, props in layout.run["props"]
            if ident == "paradise_assets.edit_project_structure" and props.action == "revert"
        ]
        check(
            "Items" in reverts and "Items/0/Name" not in reverts,
            "structural edits offer whole-list revert and no ineffective child revert",
        )
        bpy.ops.paradise_assets.save_project_settings()
        saved = canonical_toml.loads(path.read_text())
        check(
            saved["Items"] == [{"Name": "quest-key", "Kind": "Quest Item"}],
            "editing a retained row keeps prior removal and serialized enum strings",
        )

        refresh()
        structure("set", "Items/0/Limit")
        check(slot("Items/0/Limit").value_int == 7, "setting an optional uses its declared default")
        change("Items/0/Limit", 12)
        bpy.ops.paradise_assets.save_project_settings()
        refresh()
        structure("unset", "Items/0/Limit")
        structure("add", "Items")
        structure("set", "Items/1/Limit")
        structure("unset", "Items/1/Limit")
        bpy.ops.paradise_assets.save_project_settings()
        saved = canonical_toml.loads(path.read_text())
        check(
            all("Limit" not in row for row in saved["Items"]),
            "clearing optional values omits keys in saved and pending new rows",
        )

        asset_guid = "aaaaaaaa-1111-4111-8111-111111111111"
        (root / "assets" / "icon.png").write_bytes(b"fixture")
        (root / "assets" / "icon.png.meta").write_text(f'schema_version = 1\nguid = "{asset_guid}"\n')
        check(
            bpy.ops.paradise_assets.pick_asset(
                scope="document", component_id="settings", field_name="Items/0/Icon", asset=asset_guid
            )
            == {"FINISHED"},
            "project asset picker works without a prefab or active object",
        )
        bpy.ops.paradise_assets.save_project_settings()
        saved = canonical_toml.loads(path.read_text())
        check(
            saved["Items"][0]["Icon"] == {"guid": asset_guid, "path": "icon.png"},
            "project asset picker saves the selected reference inline",
        )

        refresh()
        structure("add", "Items")
        change("Items/0/Name", "temporary")
        structure("revert", "Items")
        check(
            project_settings.merged(context)["Items"] == saved["Items"],
            "list revert restores structure and all edits stored inside it",
        )

        saved["Settings"]["Speed"] = 19.0
        path.write_text(canonical_toml.dumps(saved))
        bpy.ops.paradise_assets.reload_project_settings()
        refresh()
        check(slot("Settings/Speed").value_float == 19, "reload refreshes same-path RNA widgets")

        obj = bpy.data.objects.new("Component owner", None)
        context.scene.collection.objects.link(obj)
        schema = component_schema.ComponentSchema(
            {"id": "component", "fields": [{"name": "Value", "type": "int"}]}
        )
        item = schema.plan({"Value": 6})[0]
        field_widgets.sync(context, obj, [("component", item, 6)])
        refresh()
        check(
            context.window_manager.paradise_field_slots[0].value_int == 6
            and slot("Settings/Speed").value_float == 19,
            "component and settings widget scopes coexist",
        )

        change("Items/0/Name", "pending")
        saved["Items"].reverse()
        path.write_text(canonical_toml.dumps(saved))
        disk = path.read_bytes()
        try:
            project_settings.save(context)
        except ValueError as error:
            check("changed on disk" in str(error), "external array reorder refuses indexed save")
        else:
            raise AssertionError("expected stale indexed save refusal")
        check(
            path.read_bytes() == disk and edits.count(context.window_manager) > 0,
            "conflict leaves current disk and pending work intact",
        )

        other = root / "other"
        other.mkdir()
        next_layout, _ = make_project(other)
        with patch.object(store, "project_of", return_value=next_layout):
            panel = refresh()
            check(
                "paradise_assets.discard_project_settings" in panel.operators,
                "switching projects retains pending work and exposes recovery",
            )
            bpy.ops.paradise_assets.discard_project_settings()
            check(
                bpy.ops.paradise_assets.open_project_settings(document="settings") == {"FINISHED"},
                "explicit discard lets a different project's settings open",
            )
        bpy.ops.paradise_assets.discard_project_settings()


def actual_game(root):
    source = os.environ.get("PARADISE_ASSETS_PROJECT")
    if not source:
        print("SKIP  actual game settings (set PARADISE_ASSETS_PROJECT)")
        return
    source = Path(source).resolve()
    declarations = authoring_documents.load(str(source))
    check(bool(declarations), "game declares editable project documents")
    (root / ".editor").mkdir(parents=True)
    for name in ("authoring-schema.json", "authoring-documents.json"):
        shutil.copy2(source / ".editor" / name, root / ".editor" / name)
    for declaration in declarations:
        dest = root / "assets" / declaration.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / "assets" / declaration.path, dest)
    located = ProjectLayout(str(root))
    with patch.object(store, "project_of", return_value=located):
        for document in declarations:
            bpy.ops.paradise_assets.open_project_settings(document=document.id)
            refresh()
            current = project_settings.current(bpy.context)
            payload = current[3]
            candidates = [
                item
                for item in bpy.context.window_manager.paradise_document_slots
                if item.rna in ("value_int", "value_float", "value_string")
                and edits.read_path(payload, item.path) is not None
            ]
            check(bool(candidates), f"{document.id} exposes live editable fields")
            item = candidates[0]
            value = getattr(item, item.rna)
            value = value + "-test" if isinstance(value, str) else value + 1
            target = item.path
            setattr(item, item.rna, value)
            expected = edits.read_path(project_settings.merged(bpy.context), target)
            bpy.ops.paradise_assets.save_project_settings()
            reread = authoring_documents.read(located, document)
            check(
                edits.read_path(reread, target) == expected,
                f"{document.id} copies real schema, draws, edits, and saves its source document",
            )
            bpy.ops.paradise_assets.reload_project_settings()
            refresh()
            if document.id == "game":
                count = len(project_settings.merged(bpy.context)["LootTables"]["Tables"])
                structure("add", "LootTables/Tables")
                structure("add", f"LootTables/Tables/{count}/Entries")
                bpy.ops.paradise_assets.save_project_settings()
                reread = authoring_documents.read(located, document)
                check(
                    len(reread["LootTables"]["Tables"]) == count + 1
                    and len(reread["LootTables"]["Tables"][-1]["Entries"]) == 1,
                    "actual game nested loot tables append and save with header-table structure",
                )
        bpy.ops.paradise_assets.discard_project_settings()


def main():
    paradise_assets.register()
    try:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            synthetic(root)
            actual_game(root / "game-copy")
        print("All project settings checks passed.")
    finally:
        paradise_assets.unregister()


if __name__ == "__main__":
    main()
