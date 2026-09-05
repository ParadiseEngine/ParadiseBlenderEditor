"""A stand-in for the sidecar half of ``paradise assets watch``.

The addon does not mint identities -- it writes a document and waits for the watcher's
``<file>.meta`` to appear -- so anything that CREATES a prefab is untestable without something
playing that part. This is the smallest thing that does: a thread that walks ``assets/`` and
writes a sidecar for any file lacking one, which is what C# ``SidecarMaintainer.Ensure`` does on
every file event.

It is not a mock of the addon's own code. The addon never learns it is there; it polls the
filesystem exactly as it would against the real CLI.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest

_SKIP_DIRS = frozenset({".git", ".editor", "build", "bin", "obj"})


class FakeWatcher:
    """Mints a sidecar for every unidentified file under a project's ``assets/``."""

    def __init__(self, root: str, interval: float = 0.02) -> None:
        self.root = root
        self.minted: list[str] = []
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> FakeWatcher:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)

    def reconcile(self) -> None:
        """One pass, for a test that wants the minting to have happened by a known point."""
        assets = os.path.join(self.root, "assets")
        for directory, names, files in os.walk(assets):
            names[:] = [name for name in names if name not in _SKIP_DIRS]
            for name in files:
                if name.endswith(".meta"):
                    continue
                path = os.path.join(directory, name)
                meta = path + ".meta"
                if os.path.exists(meta):
                    continue
                # Written whole then renamed, like the CLI: a reader that caught a half-written
                # sidecar would parse it as "no identity yet" and wait, which is correct but
                # would make this helper the reason a test is slow.
                temporary = meta + ".tmp"
                with open(temporary, "w", encoding="utf-8", newline="") as handle:
                    handle.write(f'schema_version = 1\nguid = "{uuid.uuid4()}"\n')
                os.replace(temporary, meta)
                self.minted.append(path)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.reconcile()
            time.sleep(self._interval)


@pytest.fixture
def watcher():
    """A factory: ``watcher(root)`` starts one on that project and stops it at teardown."""
    running: list[FakeWatcher] = []

    def start(root: str) -> FakeWatcher:
        found = FakeWatcher(root).start()
        running.append(found)
        return found

    yield start
    for found in running:
        found.stop()
