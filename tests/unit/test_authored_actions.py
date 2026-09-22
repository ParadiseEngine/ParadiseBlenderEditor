"""The generic action protocol has no game-specific names or geometry rules."""

from __future__ import annotations

import math

import pytest

from paradise_assets.document import actions, component_schema


def test_component_describes_unrelated_buttons_toggles_and_save_actions():
    schema = component_schema.ComponentSchema({"id": "example", "actions": [
        {"name": "Reindex", "displayName": "Refresh Search", "doc": "Index this shelf", "onSave": True},
        {"name": "Highlight", "kind": "toggle"},
        {"name": "Unsupported", "kind": "unknown"},
    ]})
    assert [(item.name, item.kind, item.on_save) for item in schema.actions] == [
        ("Reindex", "button", True), ("Highlight", "toggle", False)]
    assert schema.actions[0].display_name == "Refresh Search"
    assert schema.actions[0].doc == "Index this shelf"


def test_action_arguments_preserve_paths_and_toggle_values():
    command = actions.arguments("/my project/scene.prefab", "component", "Highlight", "entity",
                                "/state file.json", "/response file.json", value=False, on_save=True)
    assert command == ["assets", "invoke-action", "/my project/scene.prefab", "component", "Highlight",
                       "--entity", "entity", "--state", "/state file.json", "--response", "/response file.json",
                       "--value", "false", "--on-save"]


def test_overlay_coordinates_keep_winding_and_convert_to_blender():
    result = actions.response({"toggles": {"Highlight": True}, "documentChanged": True, "overlays": [{
        "id": "bounds", "visible": True, "vertices": [1, 2, 3, 2, 2, 3, 1, 2, 4],
        "indices": [0, 1, 2], "color": [1, 0, 0, 0.5],
    }]})
    assert result.toggles == {"Highlight": True}
    assert result.document_changed
    assert result.overlays[0].vertices == ((1, -3, 2), (2, -3, 2), (1, -4, 2))
    assert result.overlays[0].triangles == ((0, 1, 2),)


def test_hide_overlay_needs_no_geometry():
    overlay = actions.response({"overlays": [{"id": "bounds", "visible": False}]}).overlays[0]
    assert not overlay.visible


@pytest.mark.parametrize("payload", [
    [], {"toggles": {"Highlight": "true"}}, {"documentChanged": 1}, {"overlays": {}},
    {"overlays": [{"id": "test", "vertices": [0, 0, math.nan], "indices": []}]},
    {"overlays": [{"id": "test", "vertices": [0, 0, 0], "indices": [0, 0, 1]}]},
    {"overlays": [{"id": "test", "vertices": [0, 0, 0], "indices": [True, 0, 0]}]},
])
def test_invalid_response_is_rejected_before_state_changes(payload):
    with pytest.raises(ValueError):
        actions.response(payload)


def test_generated_field_is_locked_without_domain_ui():
    field = component_schema.FieldSchema({"name": "File", "type": "string", "authoredBy": "navmesh"})
    assert not field.editable
    assert not component_schema.is_asset_field(field)
