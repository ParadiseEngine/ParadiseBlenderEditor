"""Canonical TOML writing -- the Python mirror of C# ``CanonicalTomlWriter``.

**This is a cross-language contract.** The engine's writer
(``src/Paradise.Assets.Documents/CanonicalTomlWriter.cs``) and this one must produce
IDENTICAL BYTES for equivalent documents. Machine writes happen on both sides of the fence --
this addon syncs prefab documents back, the CLI's build verb rewrites them -- and only
byte-identical output keeps a round trip out of the diff. ``paradise assets prefab-check``
polices exactly that property, so a disagreement here is not a style difference: it is a failing
check on every document the addon has touched.

The spec, normative (numbering follows the C# doc so the two can be read side by side):

1. Encoding UTF-8, no BOM. Newline LF. A non-empty document ends with one LF; an empty document
   is zero bytes.
2. TOML **1.0** subset only. No comments -- a canonical write is a machine write.
3. Key order is **model order**, except that within one table every scalar and array key is
   written before any sub-table. TOML itself demands that: a ``key = value`` line after a
   ``[header]`` would belong to that header.
4. Keys are bare when non-empty and matching ``[A-Za-z0-9_-]+``, otherwise basic quoted strings.
5. Strings are basic one-line strings. Escapes ``\\"``, ``\\\\``, ``\\b``, ``\\t``, ``\\n``,
   ``\\f``, ``\\r``, and ``\\uXXXX`` (uppercase hex) for every other control character
   (U+0000-U+001F, U+007F). Never literal or multi-line strings.
6. Integers: decimal, no underscores.
7. Floats: shortest digits that round-trip, formatted by **Python's ``repr`` rules** -- which is
   why this module is short. See :func:`format_float`.
8. Booleans ``true`` / ``false``.
9. Arrays are one line: ``[1, 2, 3]`` -- ``", "`` between elements, no trailing comma, empty is
   ``[]``. Arrays hold scalars, nested arrays, or inline tables (rule 11): a table that is an
   array ELEMENT is inline by rule (ParadiseEngine#187), which is what keeps a null slot ``{}``
   expressible. A list of generic ``dict`` in the model is a different thing -- an array of
   tables, rule 10 -- and the two never mix in one value.
10. Every non-empty nested table is a ``[dotted.path]`` header; every array of tables is one
    ``[[dotted.path]]`` header per element, in element order. One blank line precedes every
    header except at the start of the document. Never dotted keys. An EMPTY generic table is
    written ``key = {}`` and an empty array of tables ``key = []``, at value position: a header
    with nothing under it has no content for the reader to restore the form from, so the only
    empty table these documents can hold is the inline one, a reference to nothing
    (ParadiseEngine#199).
11. An :class:`InlineTable` is written on one line as ``{ key = value, … }`` -- ``", "`` between
    pairs, in model order, keys by rule 4 and values by rules 5-9. An empty one is ``{}``, which
    is how a null element inside an array is spelled. Inline tables never nest another table.

    WRITING picks the form by TYPE, so a caller that builds a model controls what comes out.
    READING must rebuild the same model from the same bytes on both sides of the fence, and the
    rule is :func:`restore_inline_tables`, the mirror of C# ``TomlDocumentReader``:

    - A table at VALUE position is inline iff it is an asset reference -- empty, or exactly the
      two string keys ``guid`` and ``path`` (:func:`is_written_inline`, ParadiseEngine#187) -- a
      shape therefore RESERVED for references. Content, not syntax, because ``tomllib`` erases
      the inline/header distinction and both readers must agree on every byte.
    - A table inside an ARRAY is inline regardless of content, unless the array was spelled as
      ``[[header]]`` blocks, which stay an array of tables. ``tomllib`` erases that distinction
      too, but a ``[[`` header can only ever stand at the start of a line (rule 5 forbids the
      multi-line strings that could fake one), so :func:`header_array_paths` reads it straight
      off the text. The C# side gets the same fact from Tomlyn's ``TomlTableArray``.

The document model is plain Python: ``dict`` (insertion-ordered, which is what makes rule 3
expressible at all), ``list``, ``str``, ``bool``, ``int``, ``float``.

Imports no ``bpy``: this is format code, and it has to be testable without Blender.
"""

from __future__ import annotations

import math
import re
import tomllib

__all__ = [
    "InlineTable",
    "dump_bytes",
    "dumps",
    "format_float",
    "format_key",
    "format_value",
    "header_array_paths",
    "is_written_inline",
    "loads",
    "restore_inline_tables",
]

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")

GUID_KEY = "guid"
PATH_KEY = "path"


class InlineTable(dict):
    """A table written on one line (rule 11). A ``dict`` subclass, so every ``isinstance(x, dict)``
    in this module must exclude it: treating one as a sub-table turns ``Slots = [{…}]`` into
    ``[[Slots]]`` headers, which cannot express the null slot."""

    __slots__ = ()


def is_written_inline(table) -> bool:
    """Whether a parsed table is an asset reference: empty, or exactly ``guid`` and ``path``,
    both strings (rule 11, ParadiseEngine#187)."""
    if len(table) == 0:
        return True
    if len(table) != 2:
        return False
    return (
        GUID_KEY in table
        and PATH_KEY in table
        and isinstance(table[GUID_KEY], str)
        and isinstance(table[PATH_KEY], str)
    )


def loads(text: str) -> dict:
    """Parse canonical TOML into the document model, forms restored (rules 10 and 11).
    Raises ``tomllib.TOMLDecodeError`` as ``tomllib`` does."""
    return restore_inline_tables(tomllib.loads(text), header_array_paths(text))


_HEADER_ARRAY = re.compile(r"^\s*\[\[(.*)\]\]\s*$")


def header_array_paths(text: str) -> frozenset[tuple[str, ...]]:
    """The key paths spelled as ``[[header]]`` blocks in *text*, the one fact about form the
    reader needs that ``tomllib`` does not keep. Quoted segments are decoded by ``tomllib``
    itself rather than by a second unescaper that could disagree with it."""
    paths: set[tuple[str, ...]] = set()
    for line in text.splitlines():
        match = _HEADER_ARRAY.match(line)
        if match is None:
            continue
        try:
            parsed = tomllib.loads(f"{match.group(1)} = 0")
        except tomllib.TOMLDecodeError:
            continue  # tomllib.loads on the whole text raises the real error
        path: list[str] = []
        node = parsed
        while isinstance(node, dict):
            key, node = next(iter(node.items()))
            path.append(key)
        paths.add(tuple(path))
    return frozenset(paths)


def restore_inline_tables(value, header_arrays: frozenset[tuple[str, ...]] = frozenset(), path=()):
    """Rebuild :class:`InlineTable` values after ``tomllib``, which returns a plain ``dict`` for
    both forms; without this a read-and-write moves every reference under a header. Also the
    way a model built from JSON (the edit overlay) is made writable: a plain ``dict`` inside a
    list becomes the inline element it must be written as. *header_arrays* names the arrays
    that were spelled as ``[[header]]`` blocks (:func:`header_array_paths`)."""
    if isinstance(value, list):
        return [_restore_element(element, header_arrays, path) for element in value]
    if not isinstance(value, dict):
        return value

    restored = {
        key: restore_inline_tables(member, header_arrays, (*path, key)) for key, member in value.items()
    }

    # A nested table means structural, never a reference -- an inline table may not contain one.
    if any(_is_table(member) or _is_table_array(member) for member in restored.values()):
        return restored

    return InlineTable(restored) if is_written_inline(restored) else restored


def _restore_element(element, header_arrays, path):
    if not isinstance(element, dict):
        return restore_inline_tables(element, header_arrays, path)
    if path in header_arrays:
        return {
            key: restore_inline_tables(member, header_arrays, (*path, key))
            for key, member in element.items()
        }

    inline = InlineTable()
    for key, member in element.items():
        if _is_table(member) or _is_table_array(member):
            raise ValueError(f"nests a table inside an inline table at '{key}'")
        inline[key] = restore_inline_tables(member, header_arrays, (*path, key))
    return inline


# Rule 5's named escapes. Everything else in the control range goes to \uXXXX.
_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def dumps(document: dict) -> str:
    """Render ``document`` as canonical TOML text (LF newlines, one trailing LF)."""
    out: list[str] = []
    _write_body(out, document, prefix=None)
    return "".join(out)


def dump_bytes(document: dict) -> bytes:
    """Render ``document`` as canonical TOML bytes -- UTF-8, no BOM (rule 1)."""
    return dumps(document).encode("utf-8")


def _write_body(out: list[str], table: dict, prefix: str | None) -> None:
    """Scalars and arrays first, sub-tables after (rule 3); the caller writes the header."""
    for key, value in table.items():
        if _is_table(value) or _is_table_array(value):
            continue
        out.append(f"{format_key(key)} = {format_value(value)}\n")

    for key, value in table.items():
        formatted = format_key(key)
        path = formatted if prefix is None else f"{prefix}.{formatted}"
        if _is_table(value):
            _write_header(out, f"[{path}]")
            _write_body(out, value, path)
        elif _is_table_array(value):
            for element in value:
                _write_header(out, f"[[{path}]]")
                _write_body(out, element, path)


def _write_header(out: list[str], header: str) -> None:
    if out:
        out.append("\n")
    out.append(f"{header}\n")


def _is_table(value: object) -> bool:
    """A non-empty sub-table, written under a header. An InlineTable is a VALUE and excluded, and
    so is an empty dict: it is written ``{}`` (rule 10), since no reader could restore a header
    with nothing under it."""
    return isinstance(value, dict) and not isinstance(value, InlineTable) and len(value) > 0


def _is_table_array(value: object) -> bool:
    """A non-empty list of generic sub-tables. Empty is the array ``[]``, or the key would
    vanish (rule 10 emits one header per element); InlineTables are excluded, or the null
    slot in ``Slots = [{…}, {}]`` would vanish and shift every override onto the wrong primitive."""
    return isinstance(value, list) and len(value) > 0 and all(_is_table(e) for e in value)


def format_key(key: str) -> str:
    """A key, bare where rule 4 allows and a basic quoted string otherwise."""
    if _BARE_KEY.match(key):
        return key
    return format_string(key)


def format_value(value: object) -> str:
    """One scalar, array or inline table, per rules 5-9 and 11."""
    # bool BEFORE int: in Python bool IS an int subclass, so the obvious order writes True as 1
    # and the document silently changes type.
    if isinstance(value, bool):
        return "true" if value else "false"
    # InlineTable before any mapping test: it is a dict subclass. A plain empty dict is the same
    # bytes (rule 10).
    if isinstance(value, dict) and (isinstance(value, InlineTable) or not value):
        return format_inline_table(InlineTable(value))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format_float(value)
    if isinstance(value, str):
        return format_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(format_value(e) for e in value) + "]"
    raise TypeError(
        f"canonical TOML holds bool, int, float, str, arrays of those, tables, arrays of "
        f"tables and inline tables; got {type(value).__name__}"
    )


def format_inline_table(table: InlineTable) -> str:
    """Rule 11: ``{ key = value, … }`` on one line, model order, ``{}`` when empty."""
    if len(table) == 0:
        return "{}"

    for key, value in table.items():
        if isinstance(value, dict) or (isinstance(value, list) and any(isinstance(e, dict) for e in value)):
            raise TypeError(
                f"inline table key '{key}' holds a table; an inline table holds scalars and "
                "arrays of scalars only -- a nested one-line table is neither readable nor diffable"
            )

    return "{ " + ", ".join(f"{format_key(k)} = {format_value(v)}" for k, v in table.items()) + " }"


def format_string(value: str) -> str:
    """A basic one-line string with rule 5's escapes."""
    out = ['"']
    for char in value:
        escape = _ESCAPES.get(char)
        if escape is not None:
            out.append(escape)
        elif char < "\x20" or char == "\x7f":
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def format_float(value: float) -> str:
    """Rule 7: ``repr`` IS the specification (the C# writer copied CPython's rules so this could
    be one call; do not reimplement it). Only ``nan`` needs adjusting, since ``repr`` can sign it
    and TOML must not."""
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return repr(value)
