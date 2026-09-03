"""The Blender half: turning a scene document into objects and back.

Everything here imports ``bpy``. The format itself lives in :mod:`paradise_assets.document`,
which does not -- that split is what keeps the writer testable without Blender, and the writer is
the piece that must match the C# one byte for byte.
"""

from __future__ import annotations
