"""Logging with the Godot host's ``[Paradise.Export]`` prefix, so both exporters share one
grep-able channel; falls back to ``print`` when no operator can ``report()``.
"""

from __future__ import annotations

PREFIX = "[Paradise.Export]"

__all__ = ["error", "info", "warn"]


def info(message: str, operator=None) -> None:  # bpy.types.Operator | None
    """Progress output. Mirrors ``GD.Print``."""
    print(f"{PREFIX} {message}")
    if operator is not None:
        operator.report({"INFO"}, message)


def warn(message: str, operator=None) -> None:
    """A recoverable problem the author should fix; the export continues. Mirrors ``GD.PushWarning``."""
    print(f"{PREFIX} WARNING: {message}")
    if operator is not None:
        operator.report({"WARNING"}, message)


def error(message: str, operator=None) -> None:
    """A failure that aborted something. Mirrors ``GD.PushError``."""
    print(f"{PREFIX} ERROR: {message}")
    if operator is not None:
        operator.report({"ERROR"}, message)
