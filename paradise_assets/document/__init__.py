"""The ``*.scene`` document format -- pure Python, imports no ``bpy``.

That rule is load-bearing and not a preference. Python executes a package's ``__init__`` before
any submodule, so a top-level ``import bpy`` anywhere under here would make the whole format
layer unimportable outside Blender -- which kills the unit tests, and those are the only defence
against this addon's writer drifting from the C# one it must match byte for byte.

Everything Blender-dependent lives in :mod:`paradise_assets.materialize`.
"""

from __future__ import annotations
