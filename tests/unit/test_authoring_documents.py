from __future__ import annotations

import json
import os

import pytest

from paradise_assets.document import authoring_documents as documents
from paradise_assets.document import canonical_toml, component_schema
from paradise_assets.document.project import ProjectLayout

CONFIG = "7349a614-c04e-4e38-813e-d15a84754a24"


def project(tmp_path, declarations, components=()):
    (tmp_path / "assets").mkdir(exist_ok=True)
    (tmp_path / ".editor").mkdir(exist_ok=True)
    (tmp_path / ".editor" / documents.FILE_NAME).write_text(
        json.dumps(
            {
                "version": 1,
                "documents": declarations,
            }
        )
    )
    (tmp_path / ".editor" / "authoring-schema.json").write_text(
        json.dumps(
            {
                "version": 3,
                "components": list(components),
            }
        )
    )
    return ProjectLayout(str(tmp_path))


def test_component_settings_merge_touched_paths_into_current_document(tmp_path):
    layout = project(
        tmp_path,
        [{"id": "config", "path": "config.toml", "componentId": CONFIG}],
        [
            {
                "id": CONFIG,
                "fields": [
                    {
                        "name": "Camera",
                        "type": "object",
                        "fields": [
                            {"name": "Distance", "type": "float"},
                        ],
                    }
                ],
            }
        ],
    )
    path = tmp_path / "assets" / "config.toml"
    root = {
        "UnknownRoot": "keep",
        "Components": [
            {"Id": CONFIG, "Data": {"Camera": {"Distance": 4.0}, "Unknown": 17}},
            {"Id": "future", "Data": {"Enabled": True}},
        ],
    }
    path.write_text(canonical_toml.dumps(root))
    document = documents.load(layout.root)[0]
    schema = document.schema(component_schema.load(layout.root))
    baseline = documents.read(layout, document)
    assert baseline["Camera"]["Distance"] == 4.0

    root["Components"][0]["Data"]["ChangedElsewhere"] = 23
    path.write_text(canonical_toml.dumps(root))
    documents.save(layout, document, schema, {"Camera/Distance": 9.0}, baseline=baseline)

    saved = canonical_toml.loads(path.read_text())
    assert saved["UnknownRoot"] == "keep"
    assert saved["Components"][1] == root["Components"][1]
    assert saved["Components"][0]["Data"] == {
        "Camera": {"Distance": 9.0},
        "Unknown": 17,
        "ChangedElsewhere": 23,
    }


def test_adding_leaf_preserves_concurrent_sibling_under_previously_absent_parent(tmp_path):
    layout = project(
        tmp_path,
        [
            {
                "id": "config",
                "path": "config.toml",
                "fields": [
                    {"name": "Camera", "type": "object", "fields": [{"name": "Distance", "type": "float"}]},
                ],
            }
        ],
    )
    path = tmp_path / "assets" / "config.toml"
    path.write_text("[Camera]\nExternal = 4\n")
    document = documents.load(layout.root)[0]
    documents.save(
        layout,
        document,
        document.schema(component_schema.load(layout.root)),
        {"Camera/Distance": 9.0},
        baseline={},
    )
    assert documents.read(layout, document) == {"Camera": {"Distance": 9.0, "External": 4}}


@pytest.mark.parametrize(
    "external",
    [
        {"Items": [{"Name": "second"}, {"Name": "first"}], "Count": 1},
        {"Items": [{"Name": "first"}, {"Name": "second"}], "Count": 2},
    ],
)
def test_conflicting_array_reorder_or_same_leaf_change_refuses_save(tmp_path, external):
    layout = project(
        tmp_path,
        [
            {
                "id": "root",
                "path": "root.toml",
                "fields": [
                    {"name": "Count", "type": "int"},
                    {
                        "name": "Items",
                        "type": "array",
                        "items": {
                            "type": "object",
                            "fields": [
                                {"name": "Name", "type": "string"},
                            ],
                        },
                    },
                ],
            }
        ],
    )
    path = tmp_path / "assets" / "root.toml"
    baseline = {"Items": [{"Name": "first"}, {"Name": "second"}], "Count": 1}
    current_text = canonical_toml.dumps(external)
    path.write_text(current_text)
    document = documents.load(layout.root)[0]
    with pytest.raises(ValueError, match="changed on disk"):
        documents.save(
            layout,
            document,
            document.schema(component_schema.load(layout.root)),
            {"Items/0/Name": "edited", "Count": 3},
            baseline=baseline,
        )
    assert path.read_text() == current_text


def test_root_lists_enums_and_optional_omission_round_trip(tmp_path):
    fields = [
        {
            "name": "Items",
            "type": "array",
            "items": {
                "type": "object",
                "fields": [
                    {"name": "Id", "type": "string"},
                    {"name": "Kind", "type": "enum", "values": ["Currency", "Quest Item"]},
                    {"name": "MaxStack", "type": "int", "optional": True, "default": 1},
                ],
            },
        }
    ]
    layout = project(tmp_path, [{"id": "items", "path": "items.toml", "fields": fields}])
    path = tmp_path / "assets" / "items.toml"
    path.write_text('Untouched = 8\n[[Items]]\nId = "key"\nKind = "Quest Item"\nMaxStack = 3\n')
    document = documents.load(layout.root)[0]
    schema = document.schema(component_schema.load(layout.root))
    documents.save(layout, document, schema, {"Items/0/MaxStack": None})
    assert documents.read(layout, document) == {
        "Untouched": 8,
        "Items": [{"Id": "key", "Kind": "Quest Item"}],
    }
    documents.save(
        layout, document, schema, {"Items": [{"Id": "coin", "Kind": "Currency", "MaxStack": None}]}
    )
    assert documents.read(layout, document) == {"Untouched": 8, "Items": [{"Id": "coin", "Kind": "Currency"}]}
    with pytest.raises(ValueError, match="optional"):
        documents.save(layout, document, schema, {"Items/0/Id": None})


def test_nested_object_lists_keep_headers_and_asset_references_stay_inline(tmp_path):
    fields = [
        {
            "name": "Tables",
            "type": "array",
            "items": {
                "type": "object",
                "fields": [
                    {"name": "Name", "type": "string"},
                    {
                        "name": "Entries",
                        "type": "array",
                        "items": {
                            "type": "object",
                            "fields": [
                                {"name": "Item", "type": "string"},
                                {"name": "Icon", "type": "asset"},
                            ],
                        },
                    },
                ],
            },
        }
    ]
    layout = project(tmp_path, [{"id": "loot", "path": "loot.toml", "fields": fields}])
    path = tmp_path / "assets" / "loot.toml"
    path.write_text("Tables = []\n")
    document = documents.load(layout.root)[0]
    value = [
        {
            "Name": "common",
            "Entries": [
                {"Item": "coin", "Icon": {"guid": CONFIG, "path": "icons/coin.png"}},
                {"Item": "key", "Icon": {}},
            ],
        }
    ]
    schema = document.schema(component_schema.load(layout.root))
    documents.save(layout, document, schema, {"Tables": value})
    saved = path.read_text()
    assert "[[Tables]]" in saved and "[[Tables.Entries]]" in saved
    assert "Icon = { guid = " in saved and "Icon = {}" in saved
    assert documents.read(layout, document) == {"Tables": value}
    documents.save(layout, document, schema, {"Tables/0/Entries": value[0]["Entries"][:1]})
    assert documents.read(layout, document)["Tables"][0]["Entries"] == value[0]["Entries"][:1]


@pytest.mark.parametrize(
    "path", ["../outside.toml", "/outside.toml", "a/../outside.toml", "a\\x.toml", "C:/x.toml", "config.json"]
)
def test_manifest_refuses_paths_outside_assets_or_wrong_format(tmp_path, path):
    layout = project(tmp_path, [{"id": "bad", "path": path, "fields": []}])
    with pytest.raises(ValueError, match="assets-relative"):
        documents.load(layout.root)


def test_symlink_document_cannot_escape_assets(tmp_path):
    layout = project(tmp_path, [{"id": "linked", "path": "link.toml", "fields": []}])
    outside = tmp_path / "outside.toml"
    outside.write_text("value = 1\n")
    (tmp_path / "assets" / "link.toml").symlink_to(outside)
    with pytest.raises(ValueError, match="outside assets"):
        documents.read(layout, documents.load(layout.root)[0])


def test_manifest_component_binding_excludes_config_from_entity_add(tmp_path):
    layout = project(tmp_path, [], [{"id": CONFIG, "fields": []}])
    before = component_schema.load(layout.root)
    assert CONFIG in {schema.id for schema in component_schema.addable(before, [])}
    manifest = tmp_path / ".editor" / documents.FILE_NAME
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "documents": [
                    {"id": "config", "path": "config.toml", "componentId": CONFIG},
                ],
            }
        )
    )
    os.utime(manifest, ns=(manifest.stat().st_atime_ns, manifest.stat().st_mtime_ns + 1_000_000))
    after = component_schema.load(layout.root)
    assert CONFIG not in {schema.id for schema in component_schema.addable(after, [])}
    assert after.get(CONFIG) is not None


def test_optional_unset_field_has_no_value_widget_until_explicitly_set():
    schema = component_schema.ComponentSchema(
        {
            "id": "items",
            "fields": [
                {"name": "IconId", "type": "string", "optional": True, "default": ""},
            ],
        }
    )
    assert [(row.path, row.role) for row in schema.plan({})] == [("IconId", component_schema.ROLE_OPTIONAL)]
    assert [(row.path, row.role) for row in schema.plan({"IconId": "coin"})] == [
        ("IconId", component_schema.ROLE_OPTIONAL),
        ("IconId", component_schema.ROLE_LEAF),
    ]


def test_empty_declared_object_exposes_nested_fields_instead_of_an_asset_picker():
    schema = component_schema.ComponentSchema(
        {
            "id": "config",
            "fields": [
                {
                    "name": "Camera",
                    "type": "object",
                    "fields": [
                        {"name": "Distance", "type": "float", "default": 5},
                    ],
                },
            ],
        }
    )
    assert [(row.path, row.role) for row in schema.plan({"Camera": {}})] == [
        ("Camera/Distance", component_schema.ROLE_LEAF),
    ]


def test_unchanged_save_keeps_original_text_and_unknown_fields(tmp_path):
    layout = project(tmp_path, [{"id": "root", "path": "root.toml", "fields": []}])
    path = tmp_path / "assets" / "root.toml"
    original = '# Keep this comment until an explicit edit\nFuture = "yes"\n'
    path.write_text(original)
    declaration = documents.load(layout.root)[0]
    documents.save(layout, declaration, declaration.schema(component_schema.load(layout.root)), {})
    assert path.read_text() == original
