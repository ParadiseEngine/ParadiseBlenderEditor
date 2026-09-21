"""Unsigned and wider integers survive native RNA editing and source-document saves."""

from __future__ import annotations

import json
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
from paradise_assets.document import canonical_toml, component_schema
from paradise_assets.document.project import ProjectLayout
from paradise_assets.materialize import store

UINT_MAX = 4294967295
WIDE_VALUE = 2**60 + 123


def check(condition, label):
    assert condition, label
    print("PASS  " + label)


def refresh():
    draw(project_settings.PARADISE_ASSETS_PT_project_settings, bpy.context)


def slot(name):
    return next(s for s in bpy.context.window_manager.paradise_document_slots if s.path == name)


def main():
    paradise_assets.register()
    try:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            (root / ".editor").mkdir()
            (root / "assets/project.toml").write_text('schema_version = 1\nname = "seedtest"\n')
            fields = [
                {"name": "FilmGrainSeed", "type": "int", "minimum": 0, "maximum": UINT_MAX},
                {"name": "Seed", "type": "int", "minimum": 0, "maximum": UINT_MAX},
                {"name": "Count", "type": "int", "minimum": 0, "maximum": 100},
                {"name": "Wide", "type": "int"},
            ]
            manifest = {"version": 1, "documents": [{
                "id": "seeds", "displayName": "Seeds", "path": "settings.toml", "fields": fields,
            }]}
            (root / ".editor/authoring-documents.json").write_text(json.dumps(manifest))
            path = root / "assets/settings.toml"
            path.write_text(f"FilmGrainSeed = {UINT_MAX}\nSeed = 7\nCount = 10\nWide = {WIDE_VALUE}\n")
            with patch.object(store, "project_of", return_value=ProjectLayout(str(root))):
                bpy.ops.paradise_assets.open_project_settings(document="seeds")
                refresh()
                check(slot("FilmGrainSeed").rna == "value_integer_text"
                      and slot("FilmGrainSeed").value_integer_text == str(UINT_MAX),
                      "uint maximum loads exactly through a decimal RNA widget")
                check(slot("Seed").rna == "value_integer_text" and slot("Count").rna == "value_int",
                      "schema range chooses wide input while ordinary integers retain spinners")
                check(slot("Wide").value_integer_text == str(WIDE_VALUE),
                      "a wide current value selects decimal input without a schema range")
                original = path.read_bytes()
                bpy.ops.paradise_assets.save_project_settings()
                check(path.read_bytes() == original and edits.count(bpy.context.window_manager) == 0,
                      "loading and no-op saving wide integers changes no bytes")
                slot("Seed").value_integer_text = str(UINT_MAX)
                slot("Wide").value_integer_text = str(WIDE_VALUE + 1)
                slot("Count").value_int = 20
                bpy.ops.paradise_assets.save_project_settings()
                saved = canonical_toml.loads(path.read_text())
                check(saved["Seed"] == UINT_MAX and type(saved["Seed"]) is int
                      and saved["Wide"] == WIDE_VALUE + 1 and saved["Count"] == 20,
                      "wide values serialize as exact TOML integers alongside ordinary spinner edits")
                refresh()
                for invalid in (str(UINT_MAX + 1), "-1", "1.5", "invalid", "0xFF", ""):
                    slot("Seed").value_integer_text = invalid
                    check(bool(slot("Seed").validation_error)
                          and slot("Seed").value_integer_text == str(UINT_MAX)
                          and edits.count(bpy.context.window_manager) == 0,
                          f"invalid decimal input {invalid!r} restores the accepted value without an edit")
                bpy.ops.paradise_assets.save_project_settings()
                check(canonical_toml.loads(path.read_text())["Seed"] == UINT_MAX,
                      "rejected input cannot leak into the saved document")
                slot("Seed").value_integer_text = "0"
                check(not slot("Seed").validation_error, "valid input clears the validation message")
                bpy.ops.paradise_assets.save_project_settings()
                bpy.ops.paradise_assets.open_project_settings(document="seeds")
                refresh()
                check(slot("Seed").value_integer_text == "0"
                      and slot("FilmGrainSeed").value_integer_text == str(UINT_MAX),
                      "unsigned minimum and maximum survive save and reload")

            owner = bpy.data.objects.new("WidgetOwner", None)
            bpy.context.scene.collection.objects.link(owner)
            store.tag_object(owner, "aaaaaaaa-1111-4111-8111-111111111111", [])
            bpy.context.view_layer.objects.active = owner
            field = component_schema.FieldSchema({"name": "Value", "type": "int"})
            item = component_schema.PlanItem("Value", field, component_schema.ROLE_LEAF)
            field_widgets.sync(bpy.context, owner, [("component", item, 1)])
            check(bpy.context.window_manager.paradise_field_slots[0].rna == "value_int",
                  "component widgets use ordinary spinners for small current values")
            field_widgets.sync(bpy.context, owner, [("component", item, WIDE_VALUE)])
            component_slot = bpy.context.window_manager.paradise_field_slots[0]
            check(component_slot.rna == "value_integer_text"
                  and component_slot.value_integer_text == str(WIDE_VALUE),
                  "a changed current value rebuilds the component widget before integer overflow")
            component_slot.value_integer_text = str(WIDE_VALUE + 2)
            check(edits.edited_fields(owner, "component")["Value"] == WIDE_VALUE + 2,
                  "component decimal edits preserve Python integer values")
    finally:
        paradise_assets.unregister()
    print("\n0 failure(s)")


if __name__ == "__main__":
    main()
