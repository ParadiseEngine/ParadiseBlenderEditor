"""JSON emitter matching C# ``ExportJsonWriter`` (STJ, indented, nulls kept): float32 shortest
round-trip (``8/255`` is ``0.03137255``, not the double ``0.03137254901960784``), integral floats
bare (``5``), every array element on its own line. Writes are temp-file + atomic rename so a
half-written scene is never visible to the runtime. No ``bpy``.
"""

from __future__ import annotations

import math
import os
import stat
import struct
import tempfile
from typing import Any

__all__ = ["JsonValue", "dumps", "f32", "f32_repr", "write_json_document", "write_text_atomically"]

JsonValue = Any

_INDENT = "  "


def f32(value: float) -> float:
    """Round to the nearest IEEE-754 single, as the C# ``float`` reference path does."""
    return struct.unpack("<f", struct.pack("<f", value))[0]


def f32_repr(value: float) -> str:
    """STJ's float32 spelling: the shortest decimal that round-trips to the same single (9
    digits always suffice), integral values bare."""
    single = f32(value)

    if math.isnan(single) or math.isinf(single):
        # A NaN in exported data is an upstream bug and must not be papered over.
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
    """``1e-07`` -> STJ's ``1E-07``. Cosmetic (the contract is value-based); it keeps exports
    hand-diffable against Godot's."""
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


# Only what JSON requires; STJ's HTML escaping never applies to the paths and names carried.
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
    """Sibling temp file + ``os.replace``, so a reader never sees a truncated document. The
    temp is created mode 0600, which the document would otherwise inherit on every export: an
    existing file keeps its mode, a new one gets the ordinary umask default."""
    directory = os.path.dirname(os.path.abspath(output_path)) or "."
    os.makedirs(directory, exist_ok=True)

    handle, temp_path = tempfile.mkstemp(
        dir=directory, prefix=f".{os.path.basename(output_path)}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.chmod(temp_path, _target_mode(output_path))
        os.replace(temp_path, output_path)
    except BaseException:
        # Leaving a stray dotfile in data/ would confuse the asset pipeline's directory scans.
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def _target_mode(output_path: str) -> int:
    """The mode the written file should carry: the existing file's, else what ``open`` would
    have given a fresh one."""
    try:
        return stat.S_IMODE(os.stat(output_path).st_mode)
    except OSError:
        umask = os.umask(0)
        os.umask(umask)
        return 0o666 & ~umask
