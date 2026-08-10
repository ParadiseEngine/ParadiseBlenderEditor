"""JSON emitter that reproduces the Paradise export contract's on-disk form.

The reference implementation is C# ``Paradise.Export.Serialization.ExportJsonWriter``
(System.Text.Json, ``WriteIndented = true``, ``DefaultIgnoreCondition = Never``). Matching it
means four things Python's ``json`` module will not do on its own:

1. **PascalCase keys, nulls kept.** No key is ever omitted; a missing value is ``null``.
2. **float32 numbers.** The contract's floats are C# ``float``, printed with shortest
   round-trip precision *for 32 bits*. ``8/255`` is ``0.03137255`` in the contract but
   ``0.03137254901960784`` from a Python double -- see :func:`f32_repr`.
3. **Integral floats print bare.** STJ writes ``5``, not ``5.0``.
4. **Fully expanded arrays.** STJ's indented mode puts every array element on its own line;
   only empty collections collapse to ``[]`` / ``{}``.

Writes go through a temp file + atomic rename, mirroring
``ExportJsonWriter.WriteTextAtomically`` -- a half-written scene JSON must never be visible
to the runtime or to a live-preview reload.

Nothing here imports ``bpy``.
"""

from __future__ import annotations

import math
import os
import struct
import tempfile
from typing import Any

__all__ = ["JsonValue", "dumps", "f32", "f32_repr", "write_json_document", "write_text_atomically"]

JsonValue = Any

_INDENT = "  "


def f32(value: float) -> float:
    """Round a Python double to the nearest IEEE-754 single, returned as a double.

    Every float that reaches the contract has passed through a C# ``float`` in the reference
    implementation, so quantizing here keeps our values bit-comparable with theirs instead of
    carrying double-precision tails the engine would never see.
    """
    return struct.unpack("<f", struct.pack("<f", value))[0]


def f32_repr(value: float) -> str:
    """Format a float the way System.Text.Json formats a C# ``float``.

    STJ prints the *shortest decimal string that round-trips back to the same float32*. The
    search below is that definition executed literally: quantize to single precision, then
    try increasing significant-digit counts until parsing the result reproduces the same
    single. 9 digits always suffices for binary32.

    Integral values print without a fractional part (``5``, not ``5.0``), matching STJ and the
    golden fixtures.
    """
    single = f32(value)

    if math.isnan(single) or math.isinf(single):
        # The contract has no encoding for these; a NaN in exported data is a bug upstream
        # (an unnormalized quaternion, a divide-by-zero scale) and must not be papered over.
        raise ValueError(f"cannot serialize non-finite float: {value!r}")

    if single == int(single) and abs(single) < 1e16:
        # -0.0 would render as "-0"; STJ writes "0" for negative zero on a float.
        return str(int(single)) if single != 0.0 else "0"

    for precision in range(1, 10):
        text = f"{single:.{precision}g}"
        if f32(float(text)) == single:
            return _clean_exponent(text)

    return _clean_exponent(f"{single:.9g}")


def _clean_exponent(text: str) -> str:
    """Normalize Python's ``1e-07`` toward STJ's ``1E-07`` exponent spelling.

    Purely cosmetic: the contract is value-based, not byte-based (see the engine's
    CONVENTIONS.md), and the conformance gate compares values. Matching the spelling anyway
    keeps hand-diffing a Blender export against a Godot export practical.
    """
    if "e" not in text:
        return text
    mantissa, _, exponent = text.partition("e")
    sign = "-" if exponent.startswith("-") else "+"
    digits = exponent.lstrip("+-").zfill(2)
    return f"{mantissa}E{sign}{digits}"


def dumps(document: JsonValue) -> str:
    """Serialize to the contract's indented text form (no trailing newline)."""
    out: list[str] = []
    _write_value(document, 0, out)
    return "".join(out)


def _write_value(value: JsonValue, depth: int, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(_quote(value))
    elif isinstance(value, int):
        # bool is a subclass of int, so the True/False branches above must come first.
        out.append(str(value))
    elif isinstance(value, float):
        out.append(f32_repr(value))
    elif isinstance(value, dict):
        _write_object(value, depth, out)
    elif isinstance(value, (list, tuple)):
        _write_array(value, depth, out)
    else:
        raise TypeError(f"unsupported contract value type: {type(value).__name__}")


def _write_object(value: dict[str, JsonValue], depth: int, out: list[str]) -> None:
    if not value:
        out.append("{}")
        return
    inner = _INDENT * (depth + 1)
    out.append("{\n")
    for index, (key, item) in enumerate(value.items()):
        out.append(inner)
        out.append(_quote(key))
        out.append(": ")
        _write_value(item, depth + 1, out)
        out.append(",\n" if index < len(value) - 1 else "\n")
    out.append(_INDENT * depth)
    out.append("}")


def _write_array(value, depth: int, out: list[str]) -> None:  # list | tuple
    items = list(value)
    if not items:
        out.append("[]")
        return
    inner = _INDENT * (depth + 1)
    out.append("[\n")
    for index, item in enumerate(items):
        out.append(inner)
        _write_value(item, depth + 1, out)
        out.append(",\n" if index < len(items) - 1 else "\n")
    out.append(_INDENT * depth)
    out.append("]")


# STJ's default encoder escapes HTML-sensitive characters, but every string the contract
# carries is a path, identifier, or enum name. Escape only what JSON itself requires.
_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}


def _quote(text: str) -> str:
    out = ['"']
    for char in text:
        escape = _ESCAPES.get(char)
        if escape is not None:
            out.append(escape)
        elif char < " ":
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def write_json_document(output_path: str, document: JsonValue) -> None:
    """Serialize and atomically write, with the contract's trailing newline."""
    write_text_atomically(output_path, dumps(document) + "\n")


def write_text_atomically(output_path: str, text: str) -> None:
    """Write via a sibling temp file + ``os.replace``.

    The rename is atomic within a directory, so a reader (the runtime, or a live-preview
    reload triggered by our own file watch) sees either the previous document or the complete
    new one -- never a truncated parse error.
    """
    directory = os.path.dirname(os.path.abspath(output_path)) or "."
    os.makedirs(directory, exist_ok=True)

    handle, temp_path = tempfile.mkstemp(
        dir=directory, prefix=f".{os.path.basename(output_path)}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temp_path, output_path)
    except BaseException:
        # Leaving a stray dotfile in data/ would confuse the asset pipeline's directory scans.
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise
