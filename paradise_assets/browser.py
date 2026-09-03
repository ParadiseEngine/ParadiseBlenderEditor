"""The Asset Browser's right-click "open prefab" entry. A context menu gets an
``AssetRepresentation`` with no loaded datablock, so the prefab comes from the catalogue's
sidecar index, and everything derives from the asset's library path (not an open document) so
the menu works in a fresh file. Opening REPLACES the session, so it confirms when there is
unsaved work.
"""

from __future__ import annotations

import json
import os

import bpy
from bpy.types import Operator

from . import catalogue
from .document import project

__all__ = ["classes", "register_menu", "unregister_menu"]

MENU = "ASSETBROWSER_MT_context_menu"


def _catalogue_directory(asset) -> str | None:
    """The catalogue directory ``asset`` came from, or ``None``. String work only: this runs
    from ``poll`` and ``draw`` on every redraw."""
    if asset is None or asset.id_type != "OBJECT":
        return None

    library = str(asset.full_library_path or "")
    if not library:
        # A local asset has no library path and cannot be a catalogue entry.
        return None

    directory = os.path.dirname(library)
    tail = os.path.join(
        os.path.basename(os.path.dirname(directory)), os.path.basename(directory)
    )
    if os.path.normcase(tail) != os.path.normcase(catalogue.CATALOGUE_RELATIVE):
        return None
    return directory


def _project_root(directory: str) -> str:
    """``<root>/.editor/asset-library`` -> ``<root>``, from where the file IS so a moved project
    still resolves."""
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
        # getattr: poll is also asked in contexts with no asset.
        return _catalogue_directory(getattr(context, "asset", None)) is not None

    def invoke(self, context, event):
        # Ask only when there IS something to lose; noise teaches people to click through.
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

        # EXEC_DEFAULT: open_prefab's invoke opens a file browser, absurd for a picked file.
        return bpy.ops.paradise_assets.open_prefab("EXEC_DEFAULT", filepath=path)


def _draw(self, context) -> None:
    """Append our entry to Blender's menu, and only for assets that are ours."""
    if _catalogue_directory(getattr(context, "asset", None)) is None:
        return

    self.layout.separator()
    column = self.layout.column()
    # operator_context bypasses only the file select; invoke still asks about unsaved work.
    column.operator_context = "INVOKE_DEFAULT"
    column.operator(PARADISE_ASSETS_OT_open_asset.bl_idname, icon="FILE_BLEND")


def register_menu() -> None:
    """Append the entry if the menu exists: a renamed bundled menu must not fail the whole
    addon's registration over a context-menu entry."""
    menu = getattr(bpy.types, MENU, None)
    if menu is not None:
        menu.append(_draw)


def unregister_menu() -> None:
    menu = getattr(bpy.types, MENU, None)
    if menu is not None:
        menu.remove(_draw)


classes = (PARADISE_ASSETS_OT_open_asset,)
