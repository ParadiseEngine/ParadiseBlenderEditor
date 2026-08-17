"""Content-addressed cache for the export's expensive derived artifacts.

Measured on ShiningPie (87 mesh GLBs, 64 textures), one full export spends **3.9 s** writing the
GLBs and **36.5 s** transcoding their textures -- and 41 of the 66 KTX2 sidecars it writes are
byte-identical to another one, because a dozen props share one atlas. On top of that, every
export re-did all of it: :func:`..export.mesh._is_stale` invalidates a GLB whenever the .blend is
newer, so a single save re-encoded every texture in the project. Same for the navmesh, which
re-ran a ~3.5 s Recast bake whether or not a collider had moved. Measured end to end, an
unchanged ShiningPie re-export went from 44.4 s to 3.6 s, with every exported artifact
byte-identical either way.

This module is what makes those steps skippable. Artifacts are stored under
``<project>/.paradise-cache/<kind>/<key><ext>``, keyed by a digest of their **inputs**, so a
re-export of unchanged content is a file copy.

**The one rule: a key must be the COMPLETE input of the step it skips.** For a transcode that is
the source image bytes plus the exact command line that would encode them (see
:func:`..pipeline.ktx.encode_signature` -- deriving the key from the argv means adding a flag
invalidates the cache automatically, rather than relying on someone remembering to bump a
version constant). For a navmesh it is the serialized geometry-and-settings payload handed to the
bridge. Where an input cannot be observed cheaply and exactly -- the mesh GLBs, whose inputs are
a transitive closure over the depsgraph (modifier stacks, geometry nodes, materials, armature
actions, the exporter's own argv) -- there is deliberately **no** cache: a key that misses an
input does not fail, it ships last week's asset and reports success. That failure has already
been paid for once here, when an ``export_tangents`` fix left every GLB stale and the meshes
rendered dark.

The cache is disposable by construction. Deleting the directory only costs time, ``--force`` on
the export operator bypasses reads, and the directory self-ignores (a ``.gitignore`` holding
``*`` is written into it) so no consuming repo needs a rule for it.
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

#: Cache directory name, created next to the project's ``data/``.
DIRECTORY_NAME = ".paradise-cache"

#: Overrides the location, or disables caching entirely when set to a falsey word. Exists for
#: CI and for bisecting a suspected stale artifact without editing preferences.
LOCATION_ENV = "PARADISE_EXPORT_CACHE"

_DISABLED_VALUES = frozenset({"0", "off", "false", "no", "none"})


def digest(*parts: bytes | str) -> str:
    """SHA-256 over ``parts``, as a hex string.

    Each part is length-prefixed, so ``("ab", "c")`` and ``("a", "bc")`` hash differently. That
    matters: the parts of a real key are an image's bytes and the command line that encodes it,
    and a boundary confusion between them is exactly how two different inputs would collide onto
    one cache entry.
    """
    hasher = hashlib.sha256()
    for part in parts:
        raw = part.encode("utf-8") if isinstance(part, str) else part
        hasher.update(len(raw).to_bytes(8, "little"))
        hasher.update(raw)
    return hasher.hexdigest()


class ArtifactCache:
    """A directory of derived artifacts, addressed by input digest.

    Constructed with ``root=None`` when caching is disabled; every method then no-ops, so
    callers need no ``if cache is not None`` branches.
    """

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
        """Copy a cached artifact to ``destination``. False when there is no entry.

        A copy failure is reported and treated as a miss: the caller then does the real work,
        which is slow but correct -- the one outcome this module must never produce is a
        destination that exists but holds the wrong bytes.
        """
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
        """Take a copy of ``source`` into the cache. Failures are non-fatal by design.

        The copy lands on a temporary name in the cache directory and is then ``os.replace``d
        into place, so an entry is either complete or absent even if Blender is killed mid-write
        or two exports race. :meth:`fetch` therefore does not need to validate what it reads.
        """
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
        """Absolute path of the entry for ``key``, taking its extension from ``like``."""
        if self._root is None:
            return None
        self._prepare()
        return os.path.join(self._root, kind, key + os.path.splitext(like)[1])

    def _prepare(self) -> None:
        """Create the cache root, self-ignored, once per instance.

        The ``.gitignore`` holding ``*`` ignores the cache *and itself*, which leaves a clean
        ``git status`` in any project without that project having to know this directory exists.
        A build cache that dirties the working tree gets committed by accident exactly once.
        """
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
    """The cache for a project, from :data:`LOCATION_ENV` or beside its ``data/`` directory.

    Project-local rather than user-global: the cache mirrors one project's exported artifacts, so
    it belongs with the checkout that produced it -- and it is then deleted by the same ``rm``
    that clears any other build output.
    """
    configured = os.environ.get(LOCATION_ENV, "").strip()
    if configured.lower() in _DISABLED_VALUES:
        return ArtifactCache(None)
    if configured:
        return ArtifactCache(os.path.expanduser(configured))
    return ArtifactCache(os.path.join(paths.project_root, DIRECTORY_NAME))
