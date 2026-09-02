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
   ``[]``. Arrays hold scalars, nested arrays, or inline tables (rule 11). A list of generic
   ``dict`` is an array of tables, rule 10.
10. Every nested table is a ``[dotted.path]`` header; every array of tables is one
    ``[[dotted.path]]`` header per element, in element order. One blank line precedes every
    header except at the start of the document. Never dotted keys.
11. An :class:`InlineTable` is written on one line as ``{ key = value, … }`` -- ``", "`` between
    pairs, in model order, keys by rule 4 and values by rules 5-9. An empty one is ``{}``, which
    is how a null element inside an array is spelled. Inline tables never nest another table.

    WRITING picks the form by TYPE, so a caller that builds a model controls what comes out.
    READING restores it from CONTENT, not from the parse, because ``tomllib`` erases the
    inline/header distinction and a rule only the C# side can compute is no rule at all: both
    readers must rebuild the same model from the same bytes. The rule (ParadiseEngine#187) is
    that a table is inline iff it is an asset reference -- empty, or exactly the two string keys
    ``guid`` and ``path`` (:func:`is_written_inline`), a shape therefore RESERVED for references
    -- OR it sits inside an array, where TOML permits only the inline form. Exact rather than
    vague ("all its values are scalars"), because two implementations agreeing until the first
    document that splits them surfaces as a byte failure with nothing pointing at formatting.

    KNOWN GAP (#29): :func:`restore_inline_tables` implements only the reference half. A
    non-reference table inside an array is restored as a generic ``dict`` and re-emitted as
    ``[[header]]`` blocks, which C# does not do, and a list mixing a record row with a null
    ``{}`` row cannot be written at all. Any document holding a list of records flips form on
    the first save here until that lands.

    One consequence for rule 10: an empty table is written ``{}`` rather than under a header,
    because in these documents the only empty table that occurs is a reference to nothing.
    (The C# writer currently disagrees and emits a header; ParadiseEngine#199.)

The document model is plain Python: ``dict`` (insertion-ordered, which is what makes rule 3
expressible at all), ``list``, ``str``, ``bool``, ``int``, ``float``.

Imports no ``bpy``: this is format code, and it has to be testable without Blender.
"""

from __future__ import annotations

import math
import re

__all__ = [
    "InlineTable",
    "dump_bytes",
    "dumps",
    "format_float",
    "format_key",
    "format_value",
    "is_written_inline",
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


def restore_inline_tables(value):
    """Rebuild :class:`InlineTable` values after ``tomllib``, which returns a plain ``dict``
    for both forms; without this a read-and-write moves every reference under a header."""
    if isinstance(value, list):
        return [restore_inline_tables(element) for element in value]
    if not isinstance(value, dict):
        return value

    restored = {key: restore_inline_tables(member) for key, member in value.items()}

    # A nested table means structural, never a reference -- an inline table may not contain one.
    if any(isinstance(member, dict) and not isinstance(member, InlineTable) for member in restored.values()):
        return restored
    if any(isinstance(member, list) and _holds_tables(member) for member in restored.values()):
        return restored

    return InlineTable(restored) if is_written_inline(restored) else restored


def _holds_tables(elements) -> bool:
    return any(isinstance(e, dict) and not isinstance(e, InlineTable) for e in elements)

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
    """A sub-table, written under a header. An InlineTable is a VALUE and excluded."""
    return isinstance(value, dict) and not isinstance(value, InlineTable)


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
    # InlineTable before any mapping test: it is a dict subclass.
    if isinstance(value, InlineTable):
        return format_inline_table(value)
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
