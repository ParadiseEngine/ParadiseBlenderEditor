"""Asset pipeline: turning authored assets into what the runtime can load.

* :mod:`.ktx`    -- PNG/JPEG -> KTX2, which the engine's glTF reader requires
* :mod:`.bridge` -- locating and invoking the .NET bridge CLI (navmesh, conformance check)
* :mod:`.cache`  -- content-addressed reuse of both, so an unchanged asset is not rebuilt
* :mod:`.prune`  -- and removal of what the scene no longer references
"""

from __future__ import annotations

from . import bridge, cache, ktx, prune

__all__ = ["bridge", "cache", "ktx", "prune"]
