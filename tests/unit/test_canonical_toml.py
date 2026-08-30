"""Tests for the canonical TOML writer.

This is a cross-language contract: the C# ``CanonicalTomlWriter`` and this module must produce
identical bytes, and ``paradise-assets scene-check`` compares bytes. A regression here does not
crash -- it puts every scene the addon has touched into the diff, and fails a CI check nobody
will connect back to a formatting change.

The float rules get the most attention because they are the ones where two languages can
plausibly disagree, and the C# side deliberately adopted Python's ``repr`` so they would not.
"""

from __future__ import annotations

import math

from paradise_assets.document import canonical_toml as ct


class TestScalars:
    def test_booleans_are_lowercase_words(self):
        assert ct.format_value(True) == "true"
        assert ct.format_value(False) == "false"

    def test_a_bool_is_not_written_as_an_integer(self):
        # bool is an int subclass in Python, so a naive isinstance order writes True as 1 and
        # silently changes the document's type.
        assert ct.dumps({"flag": True}) == "flag = true\n"

    def test_integers_are_plain_decimal(self):
        assert ct.format_value(0) == "0"
        assert ct.format_value(-42) == "-42"
        assert ct.format_value(2**53) == "9007199254740992"


class TestFloats:
    def test_an_integral_float_keeps_its_point(self):
        # Without the ".0" the value reads back as an integer and the document changes type.
        assert ct.format_float(1.0) == "1.0"
        assert ct.format_float(-20.0) == "-20.0"

    def test_shortest_round_tripping_form(self):
        assert ct.format_float(0.1) == "0.1"
        assert ct.format_float(1.5) == "1.5"

    def test_negative_zero_keeps_its_sign(self):
        assert ct.format_float(-0.0) == "-0.0"

    def test_the_positional_to_exponential_boundary(self):
        # Positional while the leading digit's decimal exponent is in [-4, 16), exponential
        # outside it. These four straddle both ends.
        assert ct.format_float(1e15) == "1000000000000000.0"
        assert ct.format_float(1e16) == "1e+16"
        assert ct.format_float(1e-4) == "0.0001"
        assert ct.format_float(1e-5) == "1e-05"

    def test_specials(self):
        assert ct.format_float(math.inf) == "inf"
        assert ct.format_float(-math.inf) == "-inf"
        assert ct.format_float(math.nan) == "nan"

    def test_nan_is_never_signed(self):
        # repr can produce "-nan" on some platforms; TOML's nan carries no sign here.
        assert ct.format_float(float("-nan")) == "nan"


class TestStrings:
    def test_basic_string_with_named_escapes(self):
        assert ct.format_value('a"b\\c') == '"a\\"b\\\\c"'
        assert ct.format_value("line\nbreak\ttab") == '"line\\nbreak\\ttab"'

    def test_other_control_characters_use_uppercase_hex(self):
        assert ct.format_value("\x00\x1f\x7f") == '"\\u0000\\u001F\\u007F"'

    def test_non_ascii_is_written_literally(self):
        # The document is UTF-8; escaping would make authored prose unreadable for no gain.
        assert ct.format_value("价值") == '"价值"'


class TestKeys:
    def test_bare_where_the_grammar_allows(self):
        assert ct.format_key("schema_version") == "schema_version"
        assert ct.format_key("a-b_C9") == "a-b_C9"

    def test_quoted_otherwise(self):
        assert ct.format_key("// note") == '"// note"'
        assert ct.format_key("") == '""'
        assert ct.format_key("has.dot") == '"has.dot"'


class TestTables:
    def test_an_empty_document_is_zero_bytes(self):
        assert ct.dumps({}) == ""

    def test_scalars_are_written_before_sub_tables(self):
        # TOML demands it: a `key = value` line after a [header] would belong to that header.
        text = ct.dumps({"table": {"inner": 1}, "scalar": 2})
        assert text == "scalar = 2\n\n[table]\ninner = 1\n"

    def test_one_blank_line_precedes_every_header_but_the_first(self):
        text = ct.dumps({"a": {"x": 1}, "b": {"y": 2}})
        assert text == "[a]\nx = 1\n\n[b]\ny = 2\n"

    def test_an_empty_table_still_gets_its_header(self):
        # Presence is meaning: the key exists, and dropping the header would delete it.
        assert ct.dumps({"empty": {}}) == "[empty]\n"

    def test_nested_tables_use_dotted_headers(self):
        assert ct.dumps({"a": {"b": {"c": 1}}}) == "[a]\n\n[a.b]\nc = 1\n"

    def test_arrays_of_tables_get_one_header_each(self):
        text = ct.dumps({"items": [{"n": 1}, {"n": 2}]})
        assert text == "[[items]]\nn = 1\n\n[[items]]\nn = 2\n"

    def test_an_empty_list_is_an_array_not_an_array_of_tables(self):
        # An array-of-tables with no elements would emit nothing and the key would vanish.
        assert ct.dumps({"items": []}) == "items = []\n"


class TestArrays:
    def test_arrays_are_one_line(self):
        assert ct.format_value([1, 2, 3]) == "[1, 2, 3]"
        assert ct.format_value([1.0, -0.5]) == "[1.0, -0.5]"

    def test_nested_arrays(self):
        assert ct.format_value([[1, 2], [3]]) == "[[1, 2], [3]]"


class TestEncoding:
    def test_utf8_without_a_bom(self):
        data = ct.dump_bytes({"k": "价"})
        assert not data.startswith(b"\xef\xbb\xbf")
        assert data == 'k = "价"\n'.encode("utf-8")
