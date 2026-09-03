"""Project layout, mirroring C# ``AssetProjectLayout``: ``assets/`` is truth, ``.editor/`` and
``build/`` are derived and deletable. The ``.blend`` this addon writes must respect that: a
cache, never a second source of truth.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["ASSETS_DIR", "BUILD_DIR", "EDITOR_DIR", "MANIFEST_NAME", "SCHEMA_CANDIDATES", "SCHEMA_FILE_NAME", "ProjectLayout", "locate"]

ASSETS_DIR = "assets"
EDITOR_DIR = ".editor"

BUILD_DIR = "build"
MANIFEST_NAME = "project.toml"

SCHEMA_FILE_NAME = "authoring-schema.json"

#: Where the game's schema dump is looked for, relative to the project root, in order. A
#: launcher build writes it into ``.editor/`` (the editor cache: a function of the game's
#: records, not of ``assets/``, so ``paradise assets clean`` must not take it with ``build/``);
#: the other two are older layouts, kept so a checkout that has not rebuilt since keeps its
#: vocabulary. Both schema readers share this tuple so a fourth location cannot reach one and
#: not the other.
SCHEMA_CANDIDATES = (
    EDITOR_DIR + "/" + SCHEMA_FILE_NAME,
    BUILD_DIR + "/" + SCHEMA_FILE_NAME,
    "data/" + SCHEMA_FILE_NAME,
)


@dataclass(frozen=True)
class ProjectLayout:
    """The directories of one asset project, rooted at the directory holding ``assets/``."""

    root: str

    @property
    def assets(self) -> str:
        return os.path.join(self.root, ASSETS_DIR)

    @property
    def manifest(self) -> str:
        return os.path.join(self.assets, MANIFEST_NAME)

    @property
    def editor(self) -> str:
        return os.path.join(self.root, EDITOR_DIR)

    @property
    def editor_blend(self) -> str:
        """Materialized working ``.blend`` files -- derived from scene documents, disposable."""
        return os.path.join(self.editor, "blend")

    def blend_for(self, document_path: str) -> str:
        """The workfile for ``document_path``, mirroring its path under assets/: two documents
        can share a stem."""
        relative = os.path.relpath(document_path, self.assets)
        return os.path.join(self.editor_blend, os.path.splitext(relative)[0] + ".blend")

    def relative(self, path: str) -> str:
        """A path under ``assets/`` as the '/'-separated form documents reference it by."""
        return os.path.relpath(path, self.assets).replace(os.sep, "/")

    def resolve(self, reference: str) -> str:
        """An assets-relative document reference as an absolute path."""
        return os.path.join(self.assets, reference.replace("/", os.sep))


def locate(start: str) -> ProjectLayout | None:
    """Walk up to the first ``assets/project.toml``; the FILE is the marker because a repo can
    contain some other ``assets`` folder."""
    current = os.path.abspath(start)
    if os.path.isfile(current):
        current = os.path.dirname(current)

    while True:
        if os.path.isfile(os.path.join(current, ASSETS_DIR, MANIFEST_NAME)):
            return ProjectLayout(current)
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
