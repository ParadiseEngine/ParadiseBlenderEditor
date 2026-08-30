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
    header except at the start of the document. Empty tables still get their header -- presence
    is meaning. Never dotted keys, never inline tables.

The document model is plain Python: ``dict`` (insertion-ordered, which is what makes rule 3
expressible at all), ``list``, ``str``, ``bool``, ``int``, ``float``. A list of dicts is an
array of tables; a list of anything else is an array.

Imports no ``bpy``: this is format code, and it has to be testable without Blender.
"""

from __future__ import annotations

import math
import re

__all__ = ["dumps", "dump_bytes", "format_float", "format_key", "format_value"]

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")

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
    return isinstance(value, dict)


def _is_table_array(value: object) -> bool:
    """A non-empty list of dicts. An EMPTY list is the array ``[]``, not an array of tables.

    The distinction is not academic: rule 10 gives every array-of-tables element its own header,
    so an empty one would emit nothing at all and the key would vanish from the document.
    """
    return isinstance(value, list) and len(value) > 0 and all(isinstance(e, dict) for e in value)


def format_key(key: str) -> str:
    """A key, bare where rule 4 allows and a basic quoted string otherwise."""
    if _BARE_KEY.match(key):
        return key
    return format_string(key)


def format_value(value: object) -> str:
    """One scalar or array, per rules 5-9."""
    # bool BEFORE int: in Python bool IS an int subclass, so the obvious order writes True as 1
    # and the document silently changes type.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format_float(value)
    if isinstance(value, str):
        return format_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(format_value(e) for e in value) + "]"
    raise TypeError(
        f"canonical TOML holds bool, int, float, str, arrays of those, tables and arrays of "
        f"tables; got {type(value).__name__}"
    )


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
