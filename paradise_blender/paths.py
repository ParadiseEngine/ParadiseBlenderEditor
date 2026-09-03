"""Export path resolution (port of ``Paradise.Export.Paths.ExportPaths``). A contract field is
relative to ``data/`` and the runtime resolves it there, so an asset outside ``data/`` is
unreachable however valid the path looks in Blender; :meth:`ExportPaths.data_relative_field`
returns ``None`` for those so the caller can warn.
"""

from __future__ import annotations

import os

__all__ = ["ExportPaths", "material_file_field", "prefab_file_field"]


class ExportPaths:
    """Resolves absolute output paths under a project ``data/`` directory."""

    def __init__(self, data_dir: str) -> None:
        self._data_dir = os.path.abspath(data_dir)
        self._scenes_dir = os.path.join(self._data_dir, "scenes")

    @property
    def data_dir(self) -> str:
        return self._data_dir

    @property
    def scenes_dir(self) -> str:
        return self._scenes_dir

    @property
    def project_root(self) -> str:
        """The directory containing ``data/`` -- the Blender analogue of Godot's ``res://``."""
        return os.path.dirname(self._data_dir) or self._data_dir

    def level_data_output_path(self, scene_name: str) -> str:
        return os.path.join(self._scenes_dir, f"{scene_name}.json")

    def nav_mesh_output_path(self, scene_name: str) -> str:
        return os.path.join(self._scenes_dir, f"{scene_name}.navmesh.bin")

    def project_settings_output_path(self) -> str:
        return os.path.join(self._data_dir, "ProjectSettings.json")

    def output_path_for_field(self, field: str) -> str:
        """Absolute path for any data-relative contract field (``materials/x.json``,
        ``Models/x.glb``, ``prefabs/x.json``)."""
        return os.path.join(self._data_dir, field.replace("/", os.sep))

    def data_relative_field(self, path: str) -> str | None:
        """A path as its data-relative field, or ``None`` outside ``data/`` (module docstring)."""
        if not path or not path.strip():
            return None

        normalized = path.replace("\\", "/")
        if normalized.startswith("//"):
            # A surviving "//" means an unsaved .blend: unresolvable.
            return None

        full = os.path.abspath(
            normalized if os.path.isabs(normalized) else os.path.join(self.project_root, normalized)
        )
        relative = os.path.relpath(full, self._data_dir).replace(os.sep, "/")
        if relative == ".." or relative.startswith("../") or os.path.isabs(relative):
            return None
        return relative

    def ensure_output_directory(self) -> None:
        os.makedirs(self._data_dir, exist_ok=True)
        os.makedirs(self._scenes_dir, exist_ok=True)


def material_file_field(name_or_path: str) -> str:
    """``materials/<name>.json``: the stable id in material slots, derived the same way in both hosts."""
    normalized = name_or_path.replace("\\", "/").strip("/")
    if not normalized:
        return "materials/material.json"
    name = os.path.splitext(os.path.basename(normalized))[0] or "material"
    return f"materials/{name}.json"


def prefab_file_field(path_or_name: str) -> str:
    """Map a prefab source (collection name or .blend path) to its ``prefabs/<name>.json`` field."""
    normalized = path_or_name.replace("\\", "/").strip("/")
    if not normalized:
        return "prefabs/prefab.json"
    # Case-sensitive, matching the Godot host.
    if normalized.startswith("prefabs/"):
        normalized = normalized[len("prefabs/") :]
    return f"prefabs/{os.path.splitext(normalized)[0]}.json"
