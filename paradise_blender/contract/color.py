"""Color conversion and the contract's 8-bit ``Color32`` encoding.

Port of ``Paradise.Export.Data.Color32`` plus the sRGB transfer functions the Godot addon
gets from ``Godot.Color``. Two rules from the engine's CONVENTIONS.md drive everything here:

* **The contract is linear.** Authored colors are display-referred sRGB and must be
  linearized on the way out (Godot calls ``Color.SrgbToLinear()``; we do it explicitly).
* **The contract is 8-bit.** ``Color32`` packs each channel to a byte, so the JSON carries
  ``n/255`` values -- ``{"r": 0.03137255, ...}`` is the byte 8. Precision is genuinely lost;
  that is the contract, not an approximation on our side.

Blender is the easier source here than Godot: node-tree colors are *already linear scene
data* (Blender does its color management on display, not on the stored value). So
:func:`srgb_to_linear` is only needed for values authored through an sRGB-interpreted widget
-- see the call sites in ``export/material.py`` for which is which. Getting this backwards
double-darkens every albedo, so each caller states its reasoning.
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
    """Quantize a 0..1 float to a contract byte.

    Mirrors ``Color32.ToByte``: NaN and -inf clamp to 0, +inf to 255, and rounding is
    half-away-from-zero. Python's ``round`` is banker's rounding (round-half-to-even), which
    would disagree on exact .5 cases -- ``0.5/255`` would land on a different byte -- so the
    half-away rule is spelled out rather than delegated.
    """
    if math.isnan(value) or value == float("-inf"):
        return 0
    if value == float("inf"):
        return 255
    clamped = min(max(value, 0.0), 1.0)
    return math.floor(clamped * 255.0 + 0.5)


class Color32:
    """A contract color: four channels quantized to bytes.

    Constructed from linear float channels via :meth:`from_rgba`; serialized by
    :meth:`to_json` as ``{"r", "g", "b", "a"}`` with each channel back-expanded to
    ``byte / 255`` -- exactly what ``Color32Converter`` writes.
    """

    __slots__ = ("_bytes",)

    def __init__(self, red: int, green: int, blue: int, alpha: int) -> None:
        self._bytes = (red & 0xFF, green & 0xFF, blue & 0xFF, alpha & 0xFF)

    @classmethod
    def from_rgba(cls, red: float, green: float, blue: float, alpha: float = 1.0) -> Color32:
        return cls(to_byte(red), to_byte(green), to_byte(blue), to_byte(alpha))

    @classmethod
    def from_srgb(cls, red: float, green: float, blue: float, alpha: float = 1.0) -> Color32:
        """Linearize sRGB-authored channels, then pack. Alpha is never a color channel, so it
        does not get the transfer function (matching Godot's ``SrgbToLinear``)."""
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

    def to_json(self) -> dict[str, float]:
        return {"r": self.r, "g": self.g, "b": self.b, "a": self.a}

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Color32) and other._bytes == self._bytes

    def __hash__(self) -> int:
        return hash(self._bytes)

    def __repr__(self) -> str:
        r, g, b, a = self._bytes
        return f"Color32(r={r}, g={g}, b={b}, a={a})"


WHITE = Color32.from_rgba(1.0, 1.0, 1.0, 1.0)
BLACK = Color32.from_rgba(0.0, 0.0, 0.0, 1.0)
