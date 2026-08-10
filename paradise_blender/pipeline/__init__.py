"""Asset pipeline: turning authored assets into what the runtime can load.

* :mod:`.ktx`    -- PNG/JPEG -> KTX2, which the engine's glTF reader requires
* :mod:`.bridge` -- locating and invoking the .NET bridge CLI (navmesh, conformance check)
"""

from __future__ import annotations

from . import bridge, ktx

__all__ = ["bridge", "ktx"]
