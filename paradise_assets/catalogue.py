"""Generating the ``.blend`` that puts a project's prefabs in Blender's Asset Browser.

An asset library is a folder Blender scans for ``.blend`` files, and an asset is a DATABLOCK
inside one. A ``*.prefab`` is a TOML document, so it can never appear in the Asset Browser as
itself -- there is no asset-side equivalent of the file handler that makes dragging one in from a
file browser work. The only route is to generate datablocks that stand for the prefabs, which is
what this does.

**The catalogue is derived, and disposable.** It lives under ``.editor/``, it is regenerated from
the prefabs whenever they change, and deleting it loses nothing -- the same contract as every
other artifact there. It is not a second source of truth and nothing reads it back.

Each asset is an EMPTY carrying the prefab's path and identity, not a copy of its contents. A drop
therefore brings in one lightweight object which :mod:`dropped` converts into a real instance; an
asset that contained the geometry would append a second copy of every mesh instead, and the
document would end up referencing a datablock rather than the prefab.

The empty is why the thumbnails come from somewhere else. An empty has nothing to render, so what
the browser showed at first was one generic icon per prefab -- every asset in the library looking
identical, which is most of what an asset browser is for. :mod:`thumbnail` renders each prefab to a
PNG and this module loads it onto the empty as a CUSTOM preview, because an ID's preview image is
independent of the ID's content. The picture costs the catalogue about 1.6 KB per asset and not one
extra datablock, so it is a strictly better trade than the geometry that empty exists to avoid.
"""

from __future__ import annotations

import glob
import os

import bpy

from . import thumbnail
from .document import project
from .document.prefab import PrefabDocumentError
from .document.prefab import loads as parse_document

__all__ = ["CATALOGUE_RELATIVE", "TEMPLATE_KEY", "build", "catalogue_path", "ensure_library"]

#: Where the generated library lives, relative to the project root.
CATALOGUE_RELATIVE = os.path.join(".editor", "asset-library")

#: Names which prefab each asset stands for, written beside the catalogue it describes.
#:
#: A DROP and a right-click learn this differently, and only one of them can use
#: :data:`TEMPLATE_KEY`. A drop appends the template object, so :mod:`dropped` reads the key
#: straight off it; a context menu is handed an ``AssetRepresentation`` whose ``local_id`` is None,
#: because the datablock is not loaded and appending one merely to read a property would be a
#: bizarre thing for "open this" to do. So the mapping is written out where it can be read without
#: loading anything.
#:
#: Keyed on the asset's datablock NAME, which is exact rather than merely convenient: Blender
#: uniquifies names within a file, so two prefabs both called "Box" become ``Box`` and ``Box.001``
#: and the name the browser shows is unique by construction. That is why the key is taken from
#: ``obj.name`` after linking rather than from the name the document asked for.
INDEX_NAME = "prefabs.json"

#: Marks an object as a catalogue TEMPLATE rather than a placed instance.
#:
#: The whole point of the handshake with :mod:`dropped`: an object carrying this has been dropped
#: from the Asset Browser and is not yet part of any document. The handler converts it and clears
#: the key, so an object never carries both this and an identity.
TEMPLATE_KEY = "paradise_prefab_template"


def catalogue_path(project_root: str) -> str:
    """The .blend a project's catalogue is written to."""
    return os.path.join(project_root, CATALOGUE_RELATIVE, "prefabs.blend")


def index_path(project_root: str) -> str:
    """The sidecar naming which prefab each of the catalogue's assets stands for."""
    return os.path.join(project_root, CATALOGUE_RELATIVE, INDEX_NAME)


def library_name(project_root: str) -> str:
    """What the project's library is called in Preferences.

    Named after the project so two open projects do not fight over one entry, and so the entry
    says which tree it came from when someone finds it in Preferences a month later.
    """
    return f"Paradise — {os.path.basename(os.path.normpath(project_root))}"


def ensure_library(project_root: str) -> tuple[str, bool]:
    """Register the project's catalogue as an asset library, if it is not already.

    Returns ``(name, added)``. **Generating the catalogue and not registering it produced a file
    nothing looked at** -- the Asset Browser only ever scans the directories listed in
    Preferences, so a build that stops short of this is a build the user cannot see the result of.
    Registering is idempotent: an entry already pointing at this directory is left alone rather
    than duplicated, whatever it happens to be called.
    """
    directory = os.path.join(project_root, CATALOGUE_RELATIVE)
    os.makedirs(directory, exist_ok=True)

    wanted = os.path.normcase(os.path.abspath(directory))
    libraries = bpy.context.preferences.filepaths.asset_libraries

    for library in libraries:
        if os.path.normcase(os.path.abspath(library.path)) == wanted:
            return library.name, False

    bpy.ops.preferences.asset_library_add(directory=directory)
    # asset_library_add appends, so the new entry is the last one; naming it after the fact is
    # the only way -- the operator takes no name.
    libraries[-1].name = library_name(project_root)
    return libraries[-1].name, True


def build(project_root: str, previews: bool = True) -> tuple[int, int, list[str]]:
    """Write the catalogue for the project at ``project_root``.

    Returns ``(count, thumbnails, warnings)``.

    **Run this in its own process.** It destroys the current Blender file, and it LEAVES the
    templates behind in the session it ran in -- so opening a document afterwards in that same
    process would hand every one of them to the drop handler, which would dutifully convert them
    into instances the author never placed. Both callers (the CLI verb and the panel button)
    subprocess for this reason.

    Two phases, and the ORDER IS LOAD-BEARING. Rendering thumbnails materializes prefabs, which
    imports their GLBs; the catalogue must contain none of that. The factory reset between the
    phases is what guarantees it -- this function had to destroy the file anyway, so the firewall
    is free, but moving the render after it would quietly put the whole project's geometry into
    ``prefabs.blend``. ``tests/integration/test_prefab_thumbnails.py`` asserts it did not.
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
        # The folder under prefabs/ becomes the tag, so a library of hundreds stays navigable
        # without this needing to invent a taxonomy.
        obj.asset_data.tags.new(os.path.dirname(relative) or "prefabs")

        if previews and _apply_preview(obj, thumbnails.get(relative)):
            pictured += 1

        # obj.name, not `name`: linking may have uniquified it, and the name the browser will
        # show -- the one a context menu has to look this up by -- is the one it ended up with.
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
    """Write the asset-name -> prefab sidecar. Failing to write it must not fail the build.

    The index is a convenience for the browser's context menu; a catalogue without one still drops
    and still shows its thumbnails, and the menu simply declines to offer itself. Losing the whole
    rebuild over a sidecar would be the wrong trade.
    """
    import json

    try:
        with open(index_path(project_root), "w", encoding="utf-8") as handle:
            json.dump({"schema": 1, "assets": index}, handle, ensure_ascii=False, indent=1,
                      sort_keys=True)
    except OSError:
        pass


def _sidecar_guid(prefab_path: str) -> str | None:
    """The prefab's identity, from its sidecar -- the only place identity lives."""
    sidecar = prefab_path + ".meta"
    if not os.path.isfile(sidecar):
        return None

    with open(sidecar, encoding="utf-8") as handle:
        for line in handle:
            key, _, value = line.partition("=")
            if key.strip() == "guid":
                return value.strip().strip('"') or None
    return None


def _apply_preview(obj, png: str | None) -> bool:
    """Put ``png`` on the asset as its preview. Returns whether a real picture went on.

    ``lib_id_load_custom_preview`` sets the preview image WITHOUT touching the datablock it belongs
    to, which is the whole reason a geometry-free empty can still show its prefab. The pixels are
    saved into the catalogue and read back by the Asset Browser exactly like a generated preview.

    Falls back to asking Blender to generate one, which for an empty means a generic icon. That is
    the right outcome for a prefab that has nothing to look at -- a trigger volume, or one whose
    render failed and has already warned about it -- and it is better than leaving the asset with
    no preview at all, which draws as an empty box.
    """
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
