"""Optional component controls omit keys and respect shallow prefab override semantics."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_panel import Layout, draw

import paradise_assets
from paradise_assets import component_ops, edits, ui
from paradise_assets.document import prefab, project, well_known
from paradise_assets.document.asset_reference import AssetReference
from paradise_assets.materialize import load, save, store

COMPONENT = "7349a614-c04e-4e38-813e-d15a84754a24"
OWNER = "aaaaaaaa-1111-4111-8111-111111111111"
ROOT = "bbbbbbbb-2222-4222-8222-222222222222"
INSTANCE = "cccccccc-3333-4333-8333-333333333333"
SCHEMA = {
    "components": [
        {
            "id": COMPONENT,
            "type": "Test.Optional",
            "fields": [
                {"name": "Amount", "type": "int", "default": 4, "optional": True},
                {"name": "Missing", "type": "float", "default": 2.5, "optional": True},
                {"name": "OptionalRows", "type": "array", "optional": True, "items": {"type": "string"}},
                {
                    "name": "Options",
                    "type": "object",
                    "optional": True,
                    "fields": [
                        {"name": "Keep", "type": "int", "default": 9},
                        {"name": "Limit", "type": "int", "default": 7, "optional": True},
                    ],
                },
                {
                    "name": "Rows",
                    "type": "array",
                    "items": {
                        "type": "object",
                        "fields": [
                            {"name": "Name", "type": "string"},
                            {"name": "Limit", "type": "int", "default": 7, "optional": True},
                        ],
                    },
                },
            ],
        }
    ]
}
DATA = {
    "Amount": 3,
    "Unknown": "keep",
    "Rows": [
        {"Name": "old", "Limit": 2, "Future": "first"},
        {"Name": "next", "Limit": 3, "Future": "second"},
    ],
}


def check(condition, label):
    assert condition, label
    print("PASS  " + label)


def entry(guid, name, parent=None):
    value = prefab.PrefabObject.with_meta(guid, name, parent)
    value.components.append(
        prefab.PrefabComponent(
            well_known.TRANSFORM_ID,
            "transform",
            {
                "Position": [0, 0, 0],
                "Rotation": [0, 0, 0, 1],
                "Scale": [1, 1, 1],
            },
        )
    )
    return value


def write(path, entries):
    path.write_text(prefab.dumps(prefab.PrefabDocument(objects=entries)))


def read_data(path, guid):
    return prefab.loads(path.read_text(), str(path)).by_guid()[guid].component(COMPONENT).data


def open_document(path, guid):
    layout = project.locate(str(path))
    load.load_document(bpy.context.scene, prefab.loads(path.read_text(), str(path)), str(path), layout)
    owner = store.object_with_guid(bpy.context.scene, guid)
    bpy.context.view_layer.objects.active = owner
    return owner


def panel():
    widgets = []

    def record(_layout, owner, _property, **_kwargs):
        if hasattr(owner, "path"):
            widgets.append(owner.path)

    with patch.object(Layout, "prop", record):
        result = draw(ui.PARADISE_ASSETS_PT_object, bpy.context)
    optional = [
        (props.field_name, props.action)
        for ident, props in result.run["props"]
        if ident == "paradise_assets.edit_optional_field"
    ]
    reverts = [
        (ident, getattr(props, "field_name", ""))
        for ident, props in result.run["props"]
        if ident in ("paradise_assets.revert_component_field", "paradise_assets.revert_to_prefab")
    ]
    return widgets, optional, reverts


def optional(action, path):
    result = bpy.ops.paradise_assets.edit_optional_field(
        action=action, component_id=COMPONENT, field_name=path
    )
    check(result == {"FINISHED"}, f"{action} {path}")


def main():
    paradise_assets.register()
    try:
        with tempfile.TemporaryDirectory(prefix="paradise-optional-components-") as temporary:
            root = Path(temporary)
            (root / "assets/prefabs").mkdir(parents=True)
            (root / ".editor").mkdir()
            (root / "assets/project.toml").write_text('schema_version = 1\nname = "optional-test"\n')
            (root / ".editor/authoring-schema.json").write_text(json.dumps(SCHEMA))
            path = root / "assets/prefabs/owned.prefab"
            own = entry(OWNER, "Own")
            own.components.append(prefab.PrefabComponent(COMPONENT, "Test.Optional", copy.deepcopy(DATA)))
            write(path, [own])
            bpy.ops.wm.read_factory_settings(use_empty=True)
            owner = open_document(path, OWNER)
            widgets, controls, _ = panel()
            check(
                widgets.count("Amount") == 1
                and "Missing" not in widgets
                and ("Missing", "SET") in controls
                and ("Amount", "CLEAR") in controls,
                "optional header draws one value widget when set and Set when absent",
            )
            optional("SET", "Missing")
            widgets, _, reverts = panel()
            check(widgets.count("Missing") == 1, "setting an optional draws exactly one value widget")
            check(
                reverts.count(("paradise_assets.revert_component_field", "Missing")) == 1,
                "set optional scalar draws one pending revert control",
            )
            optional("CLEAR", "Amount")
            _, _, reverts = panel()
            check(
                reverts.count(("paradise_assets.revert_component_field", "Amount")) == 1,
                "unset optional scalar retains one pending revert control",
            )
            save.save_prefab(bpy.context.scene)
            value = read_data(path, OWNER)
            check(
                "Amount" not in value and value["Missing"] == 2.5 and value["Unknown"] == "keep",
                "owned optional clear omits its TOML key and Set saves the declared default",
            )

            optional("SET", "Options")
            _, _, reverts = panel()
            check(
                reverts.count(("paradise_assets.revert_component_field", "Options")) == 1,
                "set optional object retains one pending revert on its header",
            )
            optional("SET", "Options/Limit")
            save.save_prefab(bpy.context.scene)
            optional("CLEAR", "Options/Limit")
            save.save_prefab(bpy.context.scene)
            check(
                read_data(path, OWNER)["Options"] == {"Keep": 9},
                "owned nested optional omission preserves its object siblings",
            )
            optional("CLEAR", "Options")
            save.save_prefab(bpy.context.scene)
            check("Options" not in read_data(path, OWNER), "clearing an optional object omits the whole key")
            optional("SET", "OptionalRows")
            _, _, reverts = panel()
            check(
                reverts.count(("paradise_assets.revert_component_field", "OptionalRows")) == 1,
                "set optional array draws one pending revert control",
            )
            optional("CLEAR", "OptionalRows")
            _, _, reverts = panel()
            check(
                reverts.count(("paradise_assets.revert_component_field", "OptionalRows")) == 1,
                "unset optional array retains one pending revert control",
            )

            bpy.ops.paradise_assets.remove_array_row(component_id=COMPONENT, field_name="Rows", index=0)
            optional("CLEAR", "Rows/0/Limit")
            save.save_prefab(bpy.context.scene)
            check(
                read_data(path, OWNER)["Rows"] == [{"Name": "next", "Future": "second"}],
                "optional clear inside a pending array edit preserves row removal and unknown members",
            )
            owner = open_document(path, OWNER)
            widgets, controls, _ = panel()
            check(
                "Amount" not in widgets and ("Amount", "SET") in controls,
                "omitted component fields remain unset after save and reload",
            )

            base_path = root / "assets/prefabs/base.prefab"
            base = entry(OWNER, "Base")
            base_data = copy.deepcopy(DATA)
            base_data["Options"] = {"Keep": 9, "Limit": 7, "Future": 11}
            base.components.append(prefab.PrefabComponent(COMPONENT, "Test.Optional", base_data))
            write(base_path, [base])
            level_path = root / "assets/level.prefab"
            instance = entry(INSTANCE, "Instance", ROOT)
            instance.prefab = AssetReference("dddddddd-4444-4444-8444-444444444444", "prefabs/base.prefab")
            write(level_path, [entry(ROOT, "Level"), instance])
            owner = open_document(level_path, INSTANCE)
            _, controls, _ = panel()
            check(
                ("Amount", "CLEAR") not in controls, "inherited top-level optional has no ineffective Clear"
            )
            try:
                bpy.ops.paradise_assets.edit_optional_field(
                    action="CLEAR", component_id=COMPONENT, field_name="Amount"
                )
            except RuntimeError as error:
                check(
                    "clear it in the prefab" in str(error), "direct inherited Clear explains the format limit"
                )
            else:
                raise AssertionError("inherited top-level Clear must be refused")
            check(edits.count(owner) == 0, "refused inherited Clear creates no pending edit")

            optional("CLEAR", "Options/Limit")
            optional("CLEAR", "Rows/0/Limit")
            save.save_prefab(bpy.context.scene)
            owner = open_document(level_path, INSTANCE)
            merged = next(c["data"] for c in component_ops.components_of(owner) if c["id"] == COMPONENT)
            check(
                merged["Amount"] == 3 and merged["Options"] == {"Keep": 9, "Future": 11},
                "nested inherited omission survives reload and preserves siblings and unrelated inheritance",
            )
            check(
                merged["Rows"] == [{"Name": "old", "Future": "first"}, DATA["Rows"][1]],
                "inherited array omission preserves every other row and unknown value",
            )
            check(
                read_data(base_path, OWNER) == base_data, "instance optional editing never changes its prefab"
            )
            optional("SET", "Rows/0/Limit")
            save.save_prefab(bpy.context.scene)
            owner = open_document(level_path, INSTANCE)
            merged = next(c["data"] for c in component_ops.components_of(owner) if c["id"] == COMPONENT)
            check(
                merged["Rows"][0]["Limit"] == 7
                and merged["Rows"][0]["Future"] == "first"
                and merged["Rows"][1] == DATA["Rows"][1],
                "setting a nested inherited optional preserves its containing rows",
            )
            optional("SET", "Missing")
            optional("SET", "OptionalRows")
            save.save_prefab(bpy.context.scene)
            owner = open_document(level_path, INSTANCE)
            _, _, reverts = panel()
            check(
                reverts.count(("paradise_assets.revert_to_prefab", "Missing")) == 1,
                "set optional scalar draws one prefab revert control",
            )
            check(
                reverts.count(("paradise_assets.revert_to_prefab", "OptionalRows")) == 1,
                "set optional array draws one prefab revert control",
            )
        print("All optional component checks passed.")
    finally:
        paradise_assets.unregister()


if __name__ == "__main__":
    main()
