"""The Paradise export contract, implemented in pure Python.

This subpackage is the Blender addon's half of a two-implementation contract: the other half
is C# ``Paradise.Export`` (used by ``ParadiseGodotEditor``). Nothing here imports ``bpy``, by
design -- the contract is data and math, it is unit-tested with plain ``pytest``, and keeping
Blender out of it is what makes the conformance gate against the C# reader meaningful.

Module map:

* :mod:`.axes`          -- Blender Z-up <-> contract Y-up basis change (start here)
* :mod:`.matrix`        -- TRS composition and the column-major flatten
* :mod:`.color`         -- sRGB transfer functions and the 8-bit ``Color32``
* :mod:`.collider_fold` -- collider scale folding
* :mod:`.layers`        -- collision-layer mask -> index
* :mod:`.sky`           -- ambient irradiance / SH projection from a sky evaluator
* :mod:`.schema`        -- the document dataclasses
* :mod:`.writer`        -- the JSON emitter (float32 formatting, atomic writes)
"""

from __future__ import annotations

from . import axes, collider_fold, color, layers, matrix, schema, sky, writer
from .schema import SCHEMA_VERSION

__all__ = [
    "SCHEMA_VERSION",
    "axes",
    "collider_fold",
    "color",
    "layers",
    "matrix",
    "schema",
    "sky",
    "writer",
]
