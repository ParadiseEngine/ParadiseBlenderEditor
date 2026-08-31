"""The Asset Browser's right-click menu: opening the prefab an asset stands for.

A drop and a right-click are handed very different things, and only one of them can use
:data:`catalogue.TEMPLATE_KEY`. A drop APPENDS the template object, so :mod:`dropped` reads the key
straight off it. A context menu gets an ``AssetRepresentation`` whose ``local_id`` is ``None`` --
the datablock is not loaded, and appending one merely to read a property back off it would be a
strange thing for "open this" to do. So the mapping comes from :data:`catalogue.INDEX_NAME`, the
sidecar the build writes for exactly this.

**Everything is derived from the asset's own library path, never from an open document.** That is
what makes the menu work in a fresh file with no project open -- which is precisely the case where
"open this prefab" is the useful thing to offer, and where
:class:`ops.PARADISE_ASSETS_OT_refresh_catalogue`'s ``store.read_state`` poll would decline.

The entry opens the document the way the panel's Open does, working file and all: it hands the
path to ``paradise_assets.open_prefab``, which REPLACES the session. That is a real cost for a
right-click, so it confirms first when there is unsaved work to lose.
"""

from __future__ import annotations

import json
import os

import bpy
from bpy.types import Operator

from . import catalogue
from .document import project

__all__ = ["classes", "register_menu", "unregister_menu"]

#: The menu Blender draws when you right-click in the Asset Browser.
MENU = "ASSETBROWSER_MT_context_menu"


def _catalogue_directory(asset) -> str | None:
    """The catalogue directory ``asset`` came out of, or ``None`` when it is not one of ours.

    String work only, and deliberately so: this is what the operator's ``poll`` and the menu's
    ``draw`` both ask, and both run on every redraw. Touching the disk here would put a file read
    behind every repaint of the browser.
    """
    if asset is None or asset.id_type != "OBJECT":
        return None

    library = str(asset.full_library_path or "")
    if not library:
        # A LOCAL asset -- marked in this file rather than read from a library -- has no library
        # path. It cannot be a catalogue entry, and its datablock is right there anyway.
        return None

    directory = os.path.dirname(library)
    tail = os.path.join(
        os.path.basename(os.path.dirname(directory)), os.path.basename(directory)
    )
    if os.path.normcase(tail) != os.path.normcase(catalogue.CATALOGUE_RELATIVE):
        return None
    return directory


def _project_root(directory: str) -> str:
    """The project owning a catalogue directory: ``<root>/.editor/asset-library`` -> ``<root>``.

    Derived from where the file actually IS rather than from anything recorded inside it, so a
    project someone moved or copied still resolves against the tree it is in now.
    """
    return os.path.dirname(os.path.dirname(directory))


def _entry(directory: str, name: str) -> dict | None:
    """What the sidecar says the asset called ``name`` stands for."""
    try:
        with open(os.path.join(directory, catalogue.INDEX_NAME), encoding="utf-8") as handle:
            index = json.load(handle)
    except (OSError, ValueError):
        return None

    assets = index.get("assets") if isinstance(index, dict) else None
    if not isinstance(assets, dict):
        return None

    found = assets.get(name)
    return found if isinstance(found, dict) and found.get("path") else None


class PARADISE_ASSETS_OT_open_asset(Operator):
    """Open this prefab's document, replacing what is currently open"""

    bl_idname = "paradise_assets.open_asset"
    bl_label = "Open Prefab Document"

    @classmethod
    def poll(cls, context) -> bool:
        # getattr: `asset` only exists in the browser's context, and poll is also asked in places
        # that have no asset at all.
        return _catalogue_directory(getattr(context, "asset", None)) is not None

    def invoke(self, context, event):
        # Opening replaces the session, so unsaved work goes with it. Ask -- but only when there
        # IS something to lose: a confirmation on every right-click would be noise, and noise is
        # what teaches people to click through the one that mattered.
        if bpy.data.is_dirty:
            return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def execute(self, context):
        asset = getattr(context, "asset", None)
        directory = _catalogue_directory(asset)
        if directory is None:
            self.report({"ERROR"}, "Not a Paradise prefab asset")
            return {"CANCELLED"}

        entry = _entry(directory, asset.name)
        if entry is None:
            self.report(
                {"ERROR"},
                f"'{asset.name}' is not in {catalogue.INDEX_NAME}. The catalogue was built by an "
                "older version, or the prefab has been renamed or removed since -- rebuild it "
                "with Refresh Prefab Catalogue.",
            )
            return {"CANCELLED"}

        root = _project_root(directory)
        layout = project.locate(os.path.join(root, project.ASSETS_DIR))
        if layout is None:
            self.report({"ERROR"}, f"No asset project at {root}")
            return {"CANCELLED"}

        path = layout.resolve(entry["path"])
        if not os.path.isfile(path):
            self.report(
                {"ERROR"},
                f"{entry['path']} is in the catalogue but not on disk; rebuild the catalogue.",
            )
            return {"CANCELLED"}

        # EXEC_DEFAULT, because open_prefab's own invoke opens a file browser -- which is right
        # for the panel button and absurd here, where the file has already been picked.
        return bpy.ops.paradise_assets.open_prefab("EXEC_DEFAULT", filepath=path)


def _draw(self, context) -> None:
    """Append our entry to Blender's menu, and only for assets that are ours."""
    if _catalogue_directory(getattr(context, "asset", None)) is None:
        return

    self.layout.separator()
    column = self.layout.column()
    # So the entry runs rather than re-opening a file browser; the operator's own invoke still
    # gets to ask about unsaved work, because operator_context only bypasses the file select.
    column.operator_context = "INVOKE_DEFAULT"
    column.operator(PARADISE_ASSETS_OT_open_asset.bl_idname, icon="FILE_BLEND")


def register_menu() -> None:
    """Append the entry, if this Blender has the menu to append it to.

    Guarded rather than assumed: the menu is bundled UI and could be renamed in a future release,
    and an AttributeError raised here would fail the whole addon's registration -- taking the
    document opening, the panel and the drop handler down over a context-menu entry.
    """
    menu = getattr(bpy.types, MENU, None)
    if menu is not None:
        menu.append(_draw)


def unregister_menu() -> None:
    menu = getattr(bpy.types, MENU, None)
    if menu is not None:
        menu.remove(_draw)


classes = (PARADISE_ASSETS_OT_open_asset,)
