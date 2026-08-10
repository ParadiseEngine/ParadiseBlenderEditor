"""Launching the standalone runtime on already-exported data.

* :mod:`.host` -- runtime resolution and detached launch
* :mod:`.ops`  -- the Play operator
"""

from __future__ import annotations

from . import host, ops

__all__ = ["host", "ops"]
