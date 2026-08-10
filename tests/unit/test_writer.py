"""Tests for the contract JSON emitter -- especially float32 formatting.

The values asserted here were read out of the engine's own golden fixture,
``ParadiseEngine/src/Paradise.Export.Test/Fixtures/SampleScene.expected.json``. If these
drift, a Blender export and a Godot export of the same scene stop being comparable.
"""

from __future__ import annotations

import json
import os

import pytest

from paradise_blender.contract import writer


class TestF32Repr:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            # Straight out of the golden fixture: these are Color32 bytes expanded to floats.
            (8 / 255, "0.03137255"),
            (19 / 255, "0.07450981"),
            (49 / 255, "0.19215687"),
            (128 / 255, "0.5019608"),
            (133 / 255, "0.52156866"),
            (143 / 255, "0.56078434"),
            (1 / 255, "0.003921569"),
            (3 / 255, "0.011764706"),
            (4 / 255, "0.015686275"),
            (9 / 255, "0.03529412"),
            (11 / 255, "0.043137256"),
            (14 / 255, "0.05490196"),
        ],
    )
    def test_matches_engine_golden_fixture(self, value, expected):
        """A Python double would render ``8/255`` as 0.03137254901960784; the contract's
        float32 renders 0.03137255. Every color channel in every material depends on this."""
        assert writer.f32_repr(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0.0, "0"), (1.0, "1"), (5.0, "5"), (-3.0, "-3"), (20.0, "20"), (-0.0, "0")],
    )
    def test_integral_values_print_bare(self, value, expected):
        """System.Text.Json writes ``5``, not ``5.0`` -- matching keeps diffs clean."""
        assert writer.f32_repr(value) == expected

    def test_result_round_trips_as_float32(self):
        """The defining property: parsing the output reproduces the same single."""
        for i in range(0, 256):
            value = i / 255
            assert writer.f32(float(writer.f32_repr(value))) == writer.f32(value)

    def test_rejects_non_finite(self):
        """A NaN in exported data means a bug upstream (unnormalized quaternion, zero scale).
        Serializing it would push the failure into the engine where it is far harder to trace."""
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError, match="non-finite"):
                writer.f32_repr(bad)

    def test_small_magnitudes_use_uppercase_exponent(self):
        assert writer.f32_repr(1e-7) == "1E-07"


class TestDumps:
    def test_indentation_and_expanded_arrays(self):
        """System.Text.Json's indented mode puts every array element on its own line."""
        text = writer.dumps({"Position": [0.0, 1.0, 10.0]})
        assert text == '{\n  "Position": [\n    0,\n    1,\n    10\n  ]\n}'

    def test_empty_collections_collapse(self):
        assert writer.dumps({"Entities": [], "Overrides": {}}) == (
            '{\n  "Entities": [],\n  "Overrides": {}\n}'
        )

    def test_nulls_are_written_not_omitted(self):
        assert writer.dumps({"NavMeshFile": None}) == '{\n  "NavMeshFile": null\n}'

    def test_booleans_are_lowercase(self):
        assert writer.dumps({"IsActive": True, "SkyGradient": False}) == (
            '{\n  "IsActive": true,\n  "SkyGradient": false\n}'
        )

    def test_bool_is_not_treated_as_int(self):
        """``bool`` subclasses ``int`` in Python -- an ordering mistake in the emitter would
        turn every flag in the contract into 0/1 and change the document's type shape."""
        assert '"true"' not in writer.dumps({"x": True})
        assert writer.dumps({"x": True}).endswith('true\n}')

    def test_nested_structure(self):
        text = writer.dumps({"Camera": {"OrthographicSize": 5.0}})
        assert text == '{\n  "Camera": {\n    "OrthographicSize": 5\n  }\n}'

    def test_strings_escape_only_what_json_requires(self):
        # STJ's default encoder escapes HTML characters; contract strings are paths and
        # identifiers, so over-escaping would corrupt them on a textual comparison.
        assert writer.dumps({"Path": "materials/a&b.json"}) == '{\n  "Path": "materials/a&b.json"\n}'
        assert writer.dumps({"s": 'a"b\\c'}) == '{\n  "s": "a\\"b\\\\c"\n}'

    def test_output_parses_as_json(self):
        document = {"A": [1.5, None, True], "B": {"C": "x"}, "D": []}
        assert json.loads(writer.dumps(document)) == document

    def test_rejects_unsupported_types(self):
        with pytest.raises(TypeError, match="unsupported contract value"):
            writer.dumps({"when": object()})


class TestAtomicWrite:
    def test_writes_content_with_trailing_newline(self, tmp_path):
        target = os.path.join(tmp_path, "scenes", "sample.json")
        writer.write_json_document(target, {"SchemaVersion": 2})
        with open(target, encoding="utf-8") as handle:
            assert handle.read() == '{\n  "SchemaVersion": 2\n}\n'

    def test_creates_missing_directories(self, tmp_path):
        target = os.path.join(tmp_path, "a", "b", "c.json")
        writer.write_json_document(target, {})
        assert os.path.exists(target)

    def test_overwrites_existing(self, tmp_path):
        target = os.path.join(tmp_path, "x.json")
        writer.write_json_document(target, {"v": 1})
        writer.write_json_document(target, {"v": 2})
        with open(target, encoding="utf-8") as handle:
            assert '"v": 2' in handle.read()

    def test_leaves_no_temp_file_behind_on_failure(self, tmp_path):
        """A stray dotfile in data/ would confuse the asset pipeline's directory scans."""
        target = os.path.join(tmp_path, "x.json")
        with pytest.raises(TypeError):
            writer.write_json_document(target, {"bad": object()})
        assert os.listdir(tmp_path) == []
