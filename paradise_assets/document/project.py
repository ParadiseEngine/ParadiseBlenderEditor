"""Locating an asset project, and the directory layout inside it.

The Python mirror of ``src/Paradise.Assets.Project/AssetProjectLayout.cs``. Three trees, and the
split between them is the whole design:

* ``assets/`` -- the committed source of truth, read by tooling only.
* ``.editor/`` -- gitignored editor cache. Materialized ``.blend`` files live here.
* ``build/`` -- gitignored final output, produced by the CLI and by CI.

Deleting either derived tree loses nothing, because both are pure functions of ``assets/`` plus
the tool versions. That is the invariant the ``.blend`` this addon writes has to respect: it is
a CACHE of the scene document, never a second source of truth.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["ASSETS_DIR", "BUILD_DIR", "EDITOR_DIR", "MANIFEST_NAME", "ProjectLayout", "locate"]

ASSETS_DIR = "assets"
EDITOR_DIR = ".editor"
BUILD_DIR = "build"
MANIFEST_NAME = "project.toml"

#: The extension of an authoring scene document. No ``.toml`` half: the suffix is the format.
SCENE_SUFFIX = ".scene"


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
        """Where the ``.blend`` materialized from ``document_path`` belongs.

        Mirrors the document's path UNDER assets/, not just its filename. Every authoring document
        now ends in ``.prefab``, so ``levels/test.prefab`` and ``prefabs/test.prefab`` are two
        different files with one stem -- and a stem-keyed cache would hand the second one the
        first one's working file.
        """
        relative = os.path.relpath(document_path, self.assets)
        return os.path.join(self.editor_blend, os.path.splitext(relative)[0] + ".blend")

    def relative(self, path: str) -> str:
        """A path under ``assets/`` as the '/'-separated form documents reference it by."""
        return os.path.relpath(path, self.assets).replace(os.sep, "/")

    def resolve(self, reference: str) -> str:
        """An assets-relative document reference as an absolute path."""
        return os.path.join(self.assets, reference.replace("/", os.sep))


def locate(start: str) -> ProjectLayout | None:
    """Walk up from ``start`` looking for the first directory holding ``assets/project.toml``.

    The manifest FILE is the marker rather than the ``assets/`` directory, for the reason the C#
    original gives: a game repo can easily contain some other ``assets`` folder, and locating a
    project on that would put the whole thing in the wrong place with no error to point at.
    """
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
