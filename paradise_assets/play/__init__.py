"""Running the toolchain from Blender: build the project, and play what is open.

One external program -- the ``paradise`` CLI, found through addon preferences
(:mod:`paradise_assets.prefs`). The game's launcher is the CLI's business (``[host]`` in the
project's ``assets/project.toml``). :mod:`host` is the finding and the running; :mod:`session`
gates assets -> launcher -> play and supervises the whole session, including descendant cleanup
and stopping the game on watch rebuild failure; :mod:`ops` is the verbs as buttons.
"""

from __future__ import annotations
