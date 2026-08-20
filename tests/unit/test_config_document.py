"""Tests for the game config document reader and its surgical merge.

The property worth protecting is in :class:`TestMergePayloads`: a config file is hand-written and
holds keys no editor understands -- prose ``"// note"`` entries, and in ShiningPie's case the whole
``LootTables`` array of drop tables. A save that rebuilt the document from what a panel knows would
delete every one of them, and the loss would look exactly like a successful save. These tests pin
that the merge is additive-in-place: only the named ``Data`` objects move.
"""

from __future__ import annotations

import json

import pytest

from paradise_blender.contract import config_document, writer

# Shaped like ShiningPie's real data/shiningpie/config.json, down to the prose keys and the
# non-payload LootTables section, because those are precisely what a naive rewrite destroys.
SHINING_PIE_LIKE = json.dumps(
    {
        "// note": "Every gameplay tunable lives here. No balance constants in C#.",
        "Components": [
            {
                "Id": "shiningpie.tuning.player",
                "Data": {"MaxSpeed": 6.5, "Acceleration": 45.0, "InteractRadius": 3.0},
            },
            {
                "Id": "shiningpie.tuning.camera",
                "Data": {"OnFoot": {"YawDegrees": 130.0, "Distance": 12.0}},
            },
        ],
        "// LootTables": "MinItems..MaxItems rolled per container, entries weighted.",
        "LootTables": [
            {"Table": "Rubble", "MinItems": 0, "MaxItems": 2, "Entries": [{"Item": "metal", "Weight": 5}]}
        ],
    }
)


def document() -> dict:
    return config_document.read(SHINING_PIE_LIKE)


class TestRead:
    def test_reads_components_in_file_order(self):
        assert config_document.declared_ids(document()) == [
            "shiningpie.tuning.player",
            "shiningpie.tuning.camera",
        ]

    def test_keeps_every_key_the_addon_does_not_understand(self):
        parsed = document()
        assert parsed["// note"].startswith("Every gameplay tunable")
        assert parsed["LootTables"][0]["Table"] == "Rubble"

    def test_payload_of_returns_the_nested_data(self):
        assert config_document.payload_of(document(), "shiningpie.tuning.camera") == {
            "OnFoot": {"YawDegrees": 130.0, "Distance": 12.0}
        }

    def test_payload_of_is_none_for_an_undeclared_id(self):
        assert config_document.payload_of(document(), "shiningpie.tuning.loot") is None

    def test_a_document_with_no_components_is_still_readable(self):
        # A game whose config carries only content sections is not malformed, just empty to us.
        assert config_document.declared_ids(config_document.read('{"LootTables": []}')) == []

    def test_rejects_invalid_json(self):
        with pytest.raises(config_document.ConfigError, match="not valid JSON"):
            config_document.read("{nope}")

    def test_rejects_a_non_object_root(self):
        with pytest.raises(config_document.ConfigError, match="must be a JSON object"):
            config_document.read("[]")

    def test_rejects_components_that_are_not_an_array(self):
        with pytest.raises(config_document.ConfigError, match="must be an array"):
            config_document.read('{"Components": {}}')

    def test_rejects_an_entry_without_an_id(self):
        with pytest.raises(config_document.ConfigError, match="non-empty 'Id'"):
            config_document.read('{"Components": [{"Data": {}}]}')

    def test_rejects_an_entry_without_a_data_object(self):
        with pytest.raises(config_document.ConfigError, match="needs a 'Data' object"):
            config_document.read('{"Components": [{"Id": "game.x"}]}')

    def test_rejects_a_duplicated_id(self):
        # Whichever entry won, the other edit silently did nothing — the same refusal the
        # game-side loader makes, so both halves reject the same files.
        with pytest.raises(config_document.ConfigError, match="twice"):
            config_document.read(
                '{"Components": [{"Id": "game.x", "Data": {}}, {"Id": "game.x", "Data": {}}]}'
            )


class TestMergePayloads:
    def test_replaces_only_the_named_payload(self):
        merged = config_document.merge_payloads(
            document(), {"shiningpie.tuning.player": {"MaxSpeed": 9.0}}
        )
        assert config_document.payload_of(merged, "shiningpie.tuning.player") == {"MaxSpeed": 9.0}
        # The untouched sibling keeps its nested group intact.
        assert config_document.payload_of(merged, "shiningpie.tuning.camera") == {
            "OnFoot": {"YawDegrees": 130.0, "Distance": 12.0}
        }

    def test_preserves_prose_keys_and_content_sections(self):
        """The data-loss guard. These keys mean nothing to the addon and everything to the game."""
        merged = config_document.merge_payloads(
            document(), {"shiningpie.tuning.player": {"MaxSpeed": 9.0}}
        )
        assert merged["// note"].startswith("Every gameplay tunable")
        assert merged["// LootTables"].startswith("MinItems..MaxItems")
        assert merged["LootTables"][0]["Entries"] == [{"Item": "metal", "Weight": 5}]

    def test_preserves_top_level_key_order(self):
        # The file is read by humans; a save that reshuffled it would make every diff unreadable.
        merged = config_document.merge_payloads(document(), {})
        assert list(merged) == ["// note", "Components", "// LootTables", "LootTables"]

    def test_appends_a_group_the_file_does_not_declare_yet(self):
        # A tuning group added to the game's C# reaches the file on the first save.
        merged = config_document.merge_payloads(document(), {"shiningpie.tuning.loot": {"GridWidth": 6}})
        assert config_document.declared_ids(merged)[-1] == "shiningpie.tuning.loot"
        assert config_document.payload_of(merged, "shiningpie.tuning.loot") == {"GridWidth": 6}

    def test_does_not_mutate_the_document_it_was_given(self):
        original = document()
        config_document.merge_payloads(original, {"shiningpie.tuning.player": {"MaxSpeed": 9.0}})
        assert config_document.payload_of(original, "shiningpie.tuning.player")["MaxSpeed"] == 6.5

    def test_survives_a_full_write_and_reread(self, tmp_path):
        # End to end through the real emitter: what a Save actually does to the file.
        merged = config_document.merge_payloads(
            document(), {"shiningpie.tuning.player": {"MaxSpeed": 9.0}}
        )
        path = tmp_path / "config.json"
        writer.write_json_document(str(path), merged)

        reread = config_document.read(path.read_text(encoding="utf-8"))
        assert config_document.payload_of(reread, "shiningpie.tuning.player") == {"MaxSpeed": 9.0}
        assert reread["LootTables"][0]["Table"] == "Rubble"
        assert reread["// note"].startswith("Every gameplay tunable")


class TestConfigStamp:
    def test_a_missing_file_stamps_as_zero(self, tmp_path):
        assert config_document.config_stamp(str(tmp_path / "missing.json")) == (0, 0)

    def test_the_stamp_tracks_content_changes(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text('{"Components": []}')
        first = config_document.config_stamp(str(path))
        path.write_text('{"Components": [{"Id": "game.x", "Data": {}}]}')
        assert config_document.config_stamp(str(path)) != first


class TestDiscover:
    """What the picker offers. The filter has to be stricter than what :func:`read` accepts, or
    every JSON file under ``data/`` becomes a confusing empty row."""

    def data_tree(self, tmp_path):
        (tmp_path / "game").mkdir()
        (tmp_path / "game" / "config.json").write_text(SHINING_PIE_LIKE)

        # The three kinds of JSON a real data/ directory is full of, none of them a config.
        (tmp_path / "scenes").mkdir()
        (tmp_path / "scenes" / "level.json").write_text(
            json.dumps({"SchemaVersion": 2, "Entities": [], "Materials": []})
        )
        # The authoring schema's own list is lowercase "components" of field DECLARATIONS --
        # the closest near-miss there is, and the reason the check is case-exact.
        (tmp_path / "authoring-schema.json").write_text(
            json.dumps({"version": 2, "components": [{"id": "game.x", "fields": []}]})
        )
        (tmp_path / "ProjectSettings.json").write_text(json.dumps({"Physics": {}}))
        return tmp_path

    def test_finds_config_documents_by_data_relative_path(self, tmp_path):
        assert config_document.discover(str(self.data_tree(tmp_path))) == ["game/config.json"]

    def test_skips_scene_exports_schemas_and_settings(self, tmp_path):
        found = config_document.discover(str(self.data_tree(tmp_path)))
        assert not any("level" in f or "schema" in f or "Settings" in f for f in found)

    def test_skips_unparseable_and_non_json_files(self, tmp_path):
        tree = self.data_tree(tmp_path)
        (tree / "broken.json").write_text("{not json")
        (tree / "notes.txt").write_text(SHINING_PIE_LIKE)
        assert config_document.discover(str(tree)) == ["game/config.json"]

    def test_skips_a_components_array_that_is_empty(self, tmp_path):
        # Structurally valid but nothing to edit; offering it would be a dead row.
        (tmp_path / "empty.json").write_text(json.dumps({"Components": []}))
        assert config_document.discover(str(tmp_path)) == []

    def test_skips_entries_that_are_not_id_data_pairs(self, tmp_path):
        (tmp_path / "wrong.json").write_text(json.dumps({"Components": [{"Id": "x"}]}))
        assert config_document.discover(str(tmp_path)) == []

    def test_is_sorted_so_the_picker_order_is_stable(self, tmp_path):
        for name in ("c.json", "a.json", "b.json"):
            (tmp_path / name).write_text(SHINING_PIE_LIKE)
        assert config_document.discover(str(tmp_path)) == ["a.json", "b.json", "c.json"]

    def test_an_empty_directory_yields_nothing(self, tmp_path):
        assert config_document.discover(str(tmp_path)) == []
