"""Live preview: stream Blender edits to a running Paradise runtime.

* :mod:`.protocol`  -- the NDJSON wire format (read this first; it is the contract with the engine)
* :mod:`.transport` -- non-blocking loopback socket client
* :mod:`.session`   -- runtime process + connection lifecycle
* :mod:`.sync`      -- depsgraph updates, coalesced into patches
* :mod:`.ops`       -- start/stop/resync operators

The engine-side ``--live`` listener lives in the ``ParadiseGodotEditor`` repository and is a
separate change. Until it ships, ``tools/mock_runtime.py`` speaks this protocol so the Blender
half can be exercised end to end.
"""

from __future__ import annotations

from . import ops, protocol, session, sync, transport

__all__ = ["ops", "protocol", "session", "sync", "transport"]
