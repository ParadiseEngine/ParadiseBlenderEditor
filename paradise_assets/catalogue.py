"""The derived ``.blend`` that puts a project's prefabs in Blender's Asset Browser.

An asset must be a datablock, so each prefab becomes an EMPTY carrying its path and identity,
which :mod:`dropped` converts into an instance on drop. An asset containing the geometry would
append a second copy of every mesh and the document would reference a datablock instead of the
prefab. The empty renders as a generic icon, so :mod:`thumbnail` renders each prefab to a PNG
loaded as a custom preview (~1.6 KB per asset, no extra datablock).
"""

from __future__ import annotations

import glob
import os

import bpy

from . import thumbnail
from .document import assets, project
from .document.prefab import PrefabDocumentError
from .document.prefab import loads as parse_document

__all__ = ["CATALOGUE_RELATIVE", "TEMPLATE_KEY", "build", "catalogue_path", "ensure_library"]

CATALOGUE_RELATIVE = os.path.join(".editor", "asset-library")

#: Asset name -> prefab, for the context menu, which gets an ``AssetRepresentation`` with no
#: loaded datablock to read :data:`TEMPLATE_KEY` from. Keyed on the uniquified ``obj.name``
#: after linking, which is the name the browser shows.
INDEX_NAME = "prefabs.json"

#: Marks a dropped template not yet part of any document; :mod:`dropped` converts it and clears
#: the key, so an object never carries both this and an identity.
TEMPLATE_KEY = "paradise_prefab_template"


def catalogue_path(project_root: str) -> str:
    """The .blend a project's catalogue is written to."""
    return os.path.join(project_root, CATALOGUE_RELATIVE, "prefabs.blend")


def index_path(project_root: str) -> str:
    """The sidecar naming which prefab each of the catalogue's assets stands for."""
    return os.path.join(project_root, CATALOGUE_RELATIVE, INDEX_NAME)


def library_name(project_root: str) -> str:
    """The library's Preferences name, per project so two projects do not fight over one entry."""
    return f"Paradise — {os.path.basename(os.path.normpath(project_root))}"


def ensure_library(project_root: str) -> tuple[str, bool]:
    """Register the catalogue as an asset library (idempotent); returns ``(name, added)``.
    The browser only scans registered directories, so an unregistered catalogue is invisible."""
    directory = os.path.join(project_root, CATALOGUE_RELATIVE)
    os.makedirs(directory, exist_ok=True)

    wanted = os.path.normcase(os.path.abspath(directory))
    libraries = bpy.context.preferences.filepaths.asset_libraries

    for library in libraries:
        if os.path.normcase(os.path.abspath(library.path)) == wanted:
            return library.name, False

    bpy.ops.preferences.asset_library_add(directory=directory)
    # asset_library_add takes no name; the new entry is the last one.
    libraries[-1].name = library_name(project_root)
    return libraries[-1].name, True


def build(project_root: str, previews: bool = True) -> tuple[int, int, list[str]]:
    """Write the catalogue; returns ``(count, thumbnails, warnings)``.

    Run in its own process: it destroys the current file and leaves templates behind that a
    later document open would hand to the drop handler. Render thumbnails BEFORE the factory
    reset, or the imported GLBs end up inside ``prefabs.blend`` (pinned by
    ``test_prefab_thumbnails.py``).
    """
    layout = project.locate(os.path.join(project_root, "assets"))
    if layout is None:
        raise FileNotFoundError(f"no asset project at {project_root}")

    warnings: list[str] = []
    prefabs = sorted(glob.glob(os.path.join(layout.assets, "**", "*.prefab"), recursive=True))

    thumbnails: dict[str, str] = {}
    if previews:
        thumbnails, rendered_warnings = thumbnail.render_all(layout, prefabs)
        warnings.extend(rendered_warnings)

    bpy.ops.wm.read_factory_settings(use_empty=True)

    made = 0
    pictured = 0
    index: dict[str, dict] = {}
    for path in prefabs:
        relative = os.path.relpath(path, layout.assets).replace("\\", "/")

        try:
            with open(path, encoding="utf-8") as handle:
                document = parse_document(handle.read(), path)
        except (OSError, PrefabDocumentError) as error:
            warnings.append(f"{relative}: {error}")
            continue

        guid = _sidecar_guid(path)
        if guid is None:
            warnings.append(f"{relative}: no sidecar, so it has no identity to reference")
            continue

        root = document.root()
        name = root.name or os.path.splitext(os.path.basename(path))[0]

        obj = bpy.data.objects.new(name, None)
        obj.empty_display_size = 0.5
        obj[TEMPLATE_KEY] = _template_json(guid, relative)
        bpy.context.scene.collection.objects.link(obj)

        obj.asset_mark()
        obj.asset_data.description = f"Paradise prefab — {relative}"
        # The folder is the tag, so no taxonomy needs inventing.
        obj.asset_data.tags.new(os.path.dirname(relative) or "prefabs")

        if previews and _apply_preview(obj, thumbnails.get(relative)):
            pictured += 1

        # obj.name, not `name`: linking may have uniquified it.
        index[obj.name] = {"guid": guid, "path": relative}
        made += 1

    destination = catalogue_path(project_root)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=destination, copy=True)
    _write_index(project_root, index)

    return made, pictured, warnings


def _template_json(guid: str, relative: str) -> str:
    import json

    return json.dumps({"guid": guid, "path": relative}, ensure_ascii=False)


def _write_index(project_root: str, index: dict) -> None:
    """Write the asset-name -> prefab sidecar; a failure must not fail the build, since the
    catalogue still drops without it."""
    import json

    try:
        with open(index_path(project_root), "w", encoding="utf-8") as handle:
            json.dump({"schema": 1, "assets": index}, handle, ensure_ascii=False, indent=1,
                      sort_keys=True)
    except OSError:
        pass


def _sidecar_guid(prefab_path: str) -> str | None:
    """The prefab's identity, from its sidecar -- the only place identity lives."""
    return assets.read_sidecar_guid(prefab_path + ".meta")


def _apply_preview(obj, png: str | None) -> bool:
    """Put ``png`` on the asset as a custom preview, falling back to Blender's generic icon
    (better than no preview, which draws as an empty box). Returns whether a real picture went on."""
    if png is not None and os.path.isfile(png):
        try:
            with bpy.context.temp_override(id=obj):
                bpy.ops.ed.lib_id_load_custom_preview(filepath=png)
            return True
        except (RuntimeError, TypeError):
            pass   # fall through to the generic icon rather than leaving the asset blank

    try:
        with bpy.context.temp_override(id=obj):
            bpy.ops.ed.lib_id_generate_preview()
    except (RuntimeError, TypeError):
        pass
    return False
