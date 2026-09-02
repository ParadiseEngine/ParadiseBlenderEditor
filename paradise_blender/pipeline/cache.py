"""Content-addressed cache for KTX2 transcodes and navmesh bakes, under
``<project>/.paradise-cache/<kind>/<key><ext>``. On ShiningPie (87 GLBs, 64 textures, 36.5 s of
transcoding per export) an unchanged re-export went from 44.4 s to 3.6 s, byte-identical.

The one rule: a key must be the COMPLETE input of the step it skips (image bytes plus the encode
argv, so a flag change invalidates by construction; the whole bridge payload for a navmesh).
Mesh GLBs are deliberately NOT cached: their input is a transitive closure over the depsgraph,
and a key that misses an input ships last week's asset and reports success, which an
``export_tangents`` fix already demonstrated once by leaving every mesh dark. The engine's C#
``ArtifactCache`` shares the digest scheme but lives at ``.editor/cache`` (ParadiseEngine#204).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile

from .. import log
from ..paths import ExportPaths

__all__ = ["DIRECTORY_NAME", "ArtifactCache", "artifact_cache", "digest"]

DIRECTORY_NAME = ".paradise-cache"

#: Overrides the location, or disables caching when set to a falsey word (CI, bisecting).
LOCATION_ENV = "PARADISE_EXPORT_CACHE"

_DISABLED_VALUES = frozenset({"0", "off", "false", "no", "none"})


def digest(*parts: bytes | str) -> str:
    """SHA-256 over length-prefixed ``parts``, so ``("ab", "c")`` and ``("a", "bc")`` differ.
    Shared with the C# ``ArtifactDigest``."""
    hasher = hashlib.sha256()
    for part in parts:
        raw = part.encode("utf-8") if isinstance(part, str) else part
        hasher.update(len(raw).to_bytes(8, "little"))
        hasher.update(raw)
    return hasher.hexdigest()


class ArtifactCache:
    """Derived artifacts by input digest; ``root=None`` makes every method a no-op."""

    def __init__(self, root: str | None) -> None:
        self._root = os.path.abspath(root) if root else None
        self._prepared = False

    @property
    def enabled(self) -> bool:
        return self._root is not None

    @property
    def root(self) -> str | None:
        return self._root

    def fetch(self, kind: str, key: str, destination: str) -> bool:
        """Copy a cached artifact to ``destination``; a copy failure is a miss, never a
        destination holding the wrong bytes."""
        entry = self._entry_path(kind, key, destination)
        if entry is None or not os.path.exists(entry):
            return False

        try:
            os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
            shutil.copyfile(entry, destination)
        except OSError as error:
            log.warn(f"Export cache: could not reuse '{entry}' ({error}); regenerating.")
            return False
        return True

    def store(self, kind: str, key: str, source: str) -> None:
        """Store ``source``, temp-then-``os.replace`` so an entry is complete or absent even if
        Blender dies mid-write. Failures are non-fatal."""
        entry = self._entry_path(kind, key, source)
        if entry is None or not os.path.exists(source):
            return

        temporary = None
        try:
            os.makedirs(os.path.dirname(entry), exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=os.path.dirname(entry), suffix=".partial")
            os.close(handle)
            shutil.copyfile(source, temporary)
            os.replace(temporary, entry)
            temporary = None
        except OSError as error:
            log.warn(f"Export cache: could not store '{os.path.basename(source)}' ({error}).")
        finally:
            if temporary is not None and os.path.exists(temporary):
                with contextlib.suppress(OSError):
                    os.unlink(temporary)

    def _entry_path(self, kind: str, key: str, like: str) -> str | None:
        """Entry path, extension from ``like``. A kind must use ONE extension: storing ``.ktx2``
        and fetching with a ``.png`` destination misses silently forever."""
        if self._root is None:
            return None
        self._prepare()
        return os.path.join(self._root, kind, key + os.path.splitext(like)[1])

    def _prepare(self) -> None:
        """Create the root with a ``.gitignore`` of ``*`` (ignores itself too): a cache that
        dirties the tree gets committed by accident exactly once."""
        if self._prepared or self._root is None:
            return
        self._prepared = True
        try:
            os.makedirs(self._root, exist_ok=True)
            marker = os.path.join(self._root, ".gitignore")
            if not os.path.exists(marker):
                with open(marker, "w", encoding="utf-8") as handle:
                    handle.write("*\n")
        except OSError as error:
            log.warn(f"Export cache: '{self._root}' is unusable ({error}); caching is off.")
            self._root = None


def artifact_cache(paths: ExportPaths) -> ArtifactCache:
    """The project's cache, from :data:`LOCATION_ENV` or beside ``data/``. Project-local so it
    goes with the checkout that produced it."""
    configured = os.environ.get(LOCATION_ENV, "").strip()
    if configured.lower() in _DISABLED_VALUES:
        return ArtifactCache(None)
    if configured:
        return ArtifactCache(os.path.expanduser(configured))
    return ArtifactCache(os.path.join(paths.project_root, DIRECTORY_NAME))
