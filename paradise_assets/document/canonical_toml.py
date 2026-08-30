"""Canonical TOML writing -- the Python mirror of C# ``CanonicalTomlWriter``.

**This is a cross-language contract.** The engine's writer
(``src/Paradise.Assets.Documents/CanonicalTomlWriter.cs``) and this one must produce
IDENTICAL BYTES for equivalent documents. Machine writes happen on both sides of the fence --
this addon syncs scene documents back, the CLI's build and ``mv`` verbs rewrite them -- and only
byte-identical output keeps a round trip out of the diff. ``paradise-assets scene-check`` polices
exactly that property, so a disagreement here is not a style difference: it is a failing check on
every scene the addon has touched.

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
   ``[]``. Arrays hold scalars or nested arrays, never inline tables.
10. Every nested table is a ``[dotted.path]`` header; every array of tables is one
    ``[[dotted.path]]`` header per element, in element order. One blank line precedes every
    header except at the start of the document. Never dotted keys.
11. An :class:`InlineTable` is written on one line as ``{ key = value, … }`` -- ``", "`` between
    pairs, in model order, keys by rule 4 and values by rules 5-9. An empty one is ``{}``, which
    is how a null element inside an array is spelled. Inline tables never nest another table.

    WRITING picks the form by TYPE, so a caller that builds a model controls what comes out.
    READING cannot: TOML gives ``x = { … }`` and ``[x]`` the same parse, so the reader restores
    the type with one exact predicate -- a table is inline iff it is empty or has exactly the two
    string keys ``guid`` and ``path`` (:func:`is_reference_shaped`). That shape is therefore
    RESERVED for asset references. Exact, because a vaguer rule ("all its values are scalars")
    would have this and the C# implementation agreeing until the first document where they read it
    differently, surfacing as a ``scene-check`` byte failure with nothing pointing at formatting.

    One consequence for rule 10: an empty table is written ``{}`` rather than under a header,
    because in these documents the only empty table that occurs is a reference to nothing.

The document model is plain Python: ``dict`` (insertion-ordered, which is what makes rule 3
expressible at all), ``list``, ``str``, ``bool``, ``int``, ``float``. A list of dicts is an
array of tables; a list of anything else is an array.

Imports no ``bpy``: this is format code, and it has to be testable without Blender.
"""

from __future__ import annotations

import math
import re

__all__ = [
    "InlineTable",
    "dumps",
    "dump_bytes",
    "format_float",
    "format_key",
    "format_value",
    "is_reference_shaped",
    "restore_inline_tables",
]

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")

#: The two keys an asset reference has, in the order it writes them.
GUID_KEY = "guid"
PATH_KEY = "path"


class InlineTable(dict):
    """A table written on ONE line, ``{ key = value, … }`` -- rule 11.

    A ``dict`` subclass so it is ergonomic to build and read, but a DISTINCT TYPE so the writer
    never has to guess. Every ``isinstance(x, dict)`` test in this module therefore has to exclude
    it explicitly: an ``InlineTable`` is a VALUE, not a sub-table, and a list of them is an array
    of values rather than an array of tables. Getting that wrong turns ``Slots = [{…}]`` into
    ``[[Slots]]`` headers, which cannot express the null slot at all.
    """

    __slots__ = ()


def is_reference_shaped(table) -> bool:
    """Whether a parsed table is an asset reference, and so was written inline.

    Exact by design -- see rule 11. Empty, or exactly ``guid`` and ``path``, both strings.
    """
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
    """Rebuild :class:`InlineTable` values in something ``tomllib`` just parsed.

    ``tomllib`` returns a plain ``dict`` for both forms, so the model type is recovered here by
    the rule-11 predicate before anything writes the document back. Without this pass a document
    read and written unchanged would move every reference from ``{ … }`` to a ``[header]``, and
    ``scene-check`` would report every file as non-canonical.
    """
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

    return InlineTable(restored) if is_reference_shaped(restored) else restored


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
    """Scalars and arrays first, sub-tables after, each group in model order (rule 3).

    ``prefix`` is this table's own dotted path (``None`` at the root); writing its own header is
    the caller's job, which is what lets ``[header]`` and ``[[element]]`` share this function.
    """
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
    """A non-empty list of sub-tables. An EMPTY list is the array ``[]``, not an array of tables.

    The distinction is not academic: rule 10 gives every array-of-tables element its own header,
    so an empty one would emit nothing at all and the key would vanish from the document.

    InlineTables are excluded for a sharper reason -- ``Slots = [{…}, {}]`` is an ARRAY whose
    elements happen to be tables, and rendering it as ``[[Slots]]`` headers would lose the empty
    element entirely, silently shifting every material override onto the wrong primitive.
    """
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
    # InlineTable BEFORE the dict-free branches for the same reason it is a subclass: it must be
    # recognised as itself before anything treats it as a mapping.
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


def format_inline_table(table: "InlineTable") -> str:
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
    """A float, per rule 7.

    ``repr`` IS the specification here. The C# writer documents its float format as "formatted by
    Python's ``repr`` rules -- positional when the decimal exponent of the leading digit is in
    [-4, 16), otherwise ``d.ddde±XX``", and says the choice was made deliberately so that this
    mirror is one call. Reimplementing the rule instead of calling ``repr`` would be reimplementing
    the thing the rule was copied FROM.

    Two adjustments, both because ``repr`` is a Python spelling and TOML is not:

    * TOML has no bare ``inf`` for a Python ``float('inf')`` written as ``inf`` -- it does, and
      the spellings agree -- but ``nan`` must not carry a sign, and ``repr(float('-nan'))`` can.
    * A positional float must contain a ``.`` (rule 7), and ``repr`` of an integral value already
      gives ``1.0``. But ``repr(1e16)`` is ``'1e+16'``, which has no ``.`` and needs none: it is
      the exponential form, where TOML requires no fractional part.
    """
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return repr(value)
