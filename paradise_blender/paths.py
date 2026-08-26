"""Export output path resolution -- port of ``Paradise.Export.Paths.ExportPaths``.

The layout under the project's ``data/`` directory is shared with the Godot addon, because the
runtime resolves contract fields against it:

    data/
      scenes/<Scene>.json          level document
      scenes/<Scene>.navmesh.bin   DotRecast MeshSet
      materials/<name>.json        one document per material
      Models/<...>.glb             source meshes (KTX2 textures inside)
      primitives/<...>.glb         generated primitive meshes
      sprites/<...>.ktx2           spritesheets
      ProjectSettings.json

The one rule that actually bites: a contract field is a path **relative to the data
directory**, and the runtime resolves it as ``data/<field>``. An asset referenced from outside
``data/`` is therefore unreachable at runtime no matter how valid the path looks in Blender --
:meth:`ExportPaths.data_relative_field` returns ``None`` for those so the caller can warn
instead of exporting something that will silently fail to load.

Blender has no ``res://`` VFS, so where the Godot addon accepts ``res://`` paths this takes
absolute or project-relative filesystem paths and ``//``-prefixed Blender paths.
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
        """Map a filesystem or Blender (``//``) path to its data-relative contract field.

        Returns ``None`` when the path escapes the data directory. That is not an edge case to
        tidy away: the runtime only ever looks under ``data/``, so an out-of-tree reference is
        an asset that will not load, and the caller is expected to warn the author.
        """
        if not path or not path.strip():
            return None

        normalized = path.replace("\\", "/")
        if normalized.startswith("//"):
            # Blender's project-relative prefix, resolved against the .blend file's directory.
            # bpy.path.abspath() does this properly; the addon resolves it before calling here,
            # so a surviving "//" means an unsaved .blend and the reference is unresolvable.
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
    """Map a material name or source path to its ``materials/<name>.json`` field.

    This field is the stable id stored in entity material slots, so it must be derived the
    same way in both hosts. Distinct materials whose names collide land on the same field --
    the exporter detects that and warns rather than silently dropping one.
    """
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
    # Strip a redundant leading "prefabs/" so the field is not double-nested. Case-sensitive,
    # matching the Godot host and a case-sensitive filesystem.
    if normalized.startswith("prefabs/"):
        normalized = normalized[len("prefabs/") :]
    return f"prefabs/{os.path.splitext(normalized)[0]}.json"
