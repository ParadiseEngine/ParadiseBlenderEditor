"""Reading a ``.material`` document for DISPLAY: its ``BaseColorFactor``, and nothing else.

The game renders from the whole document; the viewport only needs to tell parts apart, and a
flat colour is what an untextured graybox looks like anyway. Imports no ``bpy``.
"""

from __future__ import annotations

import tomllib

__all__ = ["base_colour"]


def base_colour(path: str) -> tuple[float, float, float, float] | None:
    """``BaseColorFactor`` from the material document at ``path``, or ``None``."""
    try:
        with open(path, "rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None

    factor = document.get("BaseColorFactor")
    if not isinstance(factor, dict):
        return None

    return (
        float(factor.get("r", 1.0)),
        float(factor.get("g", 1.0)),
        float(factor.get("b", 1.0)),
        float(factor.get("a", 1.0)),
    )
