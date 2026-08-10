"""Authoring defaults shared with the other hosts.

Port of ``Paradise.Export.Authoring.ParadiseAuthoringDefaults``. These exist as named
constants rather than inline literals for one reason: the Unity, Godot, and Blender authoring
layers must agree on them, or the same scene authored in two tools exports different agent
data. Change these only in lockstep with the C# original.
"""

from __future__ import annotations

__all__ = [
    "ACCELERATION",
    "IDLE_ANIMATION_FALLBACK",
    "MOVE_SPEED",
    "WALK_ANIMATION_FALLBACK",
    "sanitize",
]

#: Agent movement speed, meters/second.
MOVE_SPEED = 1.4

#: Agent acceleration, meters/second^2.
ACCELERATION = 40.0

#: Clip names used when the author leaves the agent clip fields blank.
IDLE_ANIMATION_FALLBACK = "Idle"
WALK_ANIMATION_FALLBACK = "Walk"


def sanitize(value: float, fallback: float) -> float:
    """Replace a negative or non-finite authored value with its default.

    Mirrors ``EntityExport.Sanitize``. Blender's UI can produce NaN through driver
    expressions, and a NaN move speed would reach the simulation and stall the agent
    permanently rather than failing loudly.
    """
    if value != value:  # NaN
        return fallback
    if value in (float("inf"), float("-inf")):
        return fallback
    return value if value >= 0.0 else fallback
