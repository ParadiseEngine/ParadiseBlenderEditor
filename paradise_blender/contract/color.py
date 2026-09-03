"""Colour conversion and the contract's 8-bit ``Color32`` (port of ``Paradise.Export.Data.Color32``).
The contract is linear and 8-bit. Blender node colours are ALREADY linear, so
:func:`srgb_to_linear` is only for values authored through an sRGB-interpreted widget; getting
this backwards double-darkens every albedo.
"""

from __future__ import annotations

import math

from .writer import f32

__all__ = ["Color32", "linear_to_srgb", "srgb_to_linear", "to_byte"]

RgbaTuple = tuple[float, float, float, float]


def srgb_to_linear(value: float) -> float:
    """sRGB EOTF, matching ``Godot.Color.SrgbToLinear`` piecewise-exactly."""
    return value / 12.92 if value < 0.04045 else math.pow((value + 0.055) / 1.055, 2.4)


def linear_to_srgb(value: float) -> float:
    """Inverse of :func:`srgb_to_linear` (``Godot.Color.LinearToSrgb``)."""
    if value < 0.0031308:
        return value * 12.92
    return 1.055 * math.pow(value, 1.0 / 2.4) - 0.055


def to_byte(value: float) -> int:
    """``Color32.ToByte``: half-away-from-zero, spelled out because Python's ``round`` is
    banker's rounding and would land ``0.5/255`` on a different byte."""
    if math.isnan(value) or value == float("-inf"):
        return 0
    if value == float("inf"):
        return 255
    clamped = min(max(value, 0.0), 1.0)
    return math.floor(clamped * 255.0 + 0.5)


class Color32:
    """A contract colour: four channels quantized to bytes, serialized as ``"#RRGGBBAA"``."""

    __slots__ = ("_bytes",)

    def __init__(self, red: int, green: int, blue: int, alpha: int) -> None:
        self._bytes = (red & 0xFF, green & 0xFF, blue & 0xFF, alpha & 0xFF)

    @classmethod
    def from_rgba(cls, red: float, green: float, blue: float, alpha: float = 1.0) -> Color32:
        return cls(to_byte(red), to_byte(green), to_byte(blue), to_byte(alpha))

    @classmethod
    def from_srgb(cls, red: float, green: float, blue: float, alpha: float = 1.0) -> Color32:
        """Linearize sRGB-authored channels, then pack; alpha gets no transfer function."""
        return cls.from_rgba(srgb_to_linear(red), srgb_to_linear(green), srgb_to_linear(blue), alpha)

    @property
    def r(self) -> float:
        return f32(self._bytes[0] / 255.0)

    @property
    def g(self) -> float:
        return f32(self._bytes[1] / 255.0)

    @property
    def b(self) -> float:
        return f32(self._bytes[2] / 255.0)

    @property
    def a(self) -> float:
        return f32(self._bytes[3] / 255.0)

    def to_json(self) -> str:
        """``"#RRGGBBAA"``, always nine characters. A string rather than an ``{r,g,b,a}``
        object because an object would need a second reserved inline-table shape in the
        canonical TOML writer; a scalar needs none. Mirrors ``Color32Converter``."""
        return "#{:02X}{:02X}{:02X}{:02X}".format(*self._bytes)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Color32) and other._bytes == self._bytes

    def __hash__(self) -> int:
        return hash(self._bytes)

    def __repr__(self) -> str:
        r, g, b, a = self._bytes
        return f"Color32(r={r}, g={g}, b={b}, a={a})"


WHITE = Color32.from_rgba(1.0, 1.0, 1.0, 1.0)
BLACK = Color32.from_rgba(0.0, 0.0, 0.0, 1.0)
