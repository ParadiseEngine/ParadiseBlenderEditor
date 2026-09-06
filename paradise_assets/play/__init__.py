"""Running the toolchain from Blender: build the project, and play what is open.

One external program -- the ``paradise`` CLI, found through addon preferences
(:mod:`paradise_assets.prefs`). The game's launcher is the CLI's business (``[host]`` in the
project's ``assets/project.toml``). :mod:`host` is the finding and the running; :mod:`session` is
the game as a supervised child; :mod:`ops` is the verbs as buttons.
"""

from __future__ import annotations
