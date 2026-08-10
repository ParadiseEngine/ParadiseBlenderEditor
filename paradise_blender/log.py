"""Logging with message parity against the Godot addon.

Every diagnostic the Godot host emits is prefixed ``[Paradise.Export]``. Keeping the same
prefix here means a developer grepping build output, or comparing a Blender export against a
Godot export of the same scene, sees one consistent channel rather than two dialects.

Blender has no ``GD.PushWarning`` equivalent that works outside an operator, so these fall
back to ``print`` when no operator is available to ``report()`` through. Warnings additionally
go through ``warnings``-free plain output because Blender's console is where authors actually
look.
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
    """A recoverable problem the author should fix. Mirrors ``GD.PushWarning``.

    Used for lossy or unreachable data -- an asset outside ``data/``, a multi-layer collider,
    a material name collision. The export continues; the affected data does not reach the
    runtime intact.
    """
    print(f"{PREFIX} WARNING: {message}")
    if operator is not None:
        operator.report({"WARNING"}, message)


def error(message: str, operator=None) -> None:
    """A failure that aborted something. Mirrors ``GD.PushError``."""
    print(f"{PREFIX} ERROR: {message}")
    if operator is not None:
        operator.report({"ERROR"}, message)
