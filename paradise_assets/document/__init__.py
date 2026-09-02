"""The ``*.prefab`` document format, pure Python. No ``bpy`` anywhere under here: the unit
tests are the only defence against the writer drifting from the C# one it must match byte for
byte, and a top-level ``import bpy`` would kill them."""

from __future__ import annotations
