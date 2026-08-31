"""Running the toolchain from Blender: build the project, and play what is open.

Two external programs -- the ``paradise`` CLI and the game's own launcher -- found through addon
preferences (:mod:`paradise_assets.prefs`) rather than project settings, and run in opposite ways.
:mod:`host` is the finding and the running; :mod:`ops` is the four verbs as buttons.
"""

from __future__ import annotations
