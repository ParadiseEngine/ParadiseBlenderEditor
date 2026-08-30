"""The "Paradise Assets" sidebar tab.

Two panels: what document this scene came from and what can be done to it, and what the selected
object is in that document. The second is READ-ONLY by design -- component payloads pass through
untouched, and a panel that let you type into them would be promising an edit that never reaches
the file.
"""

from __future__ import annotations

import os

import bpy
from bpy.types import Panel

from .materialize import store

__all__ = ["classes"]


class _AssetsPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Paradise Assets"


class PARADISE_ASSETS_PT_document(_AssetsPanel, Panel):
    bl_label = "Prefab Document"
    bl_idname = "PARADISE_ASSETS_PT_document"

    def draw(self, context):
        layout = self.layout
        state = store.read_state(context.scene)

        if state is None:
            layout.label(text="No prefab document loaded.", icon="INFO")
            layout.operator("paradise_assets.open_prefab", icon="FILE_FOLDER")
            return

        box = layout.box()
        box.label(text=os.path.basename(state.path), icon="FILE_TEXT")
        box.label(text=os.path.dirname(state.path))

        count = sum(1 for obj in context.scene.collection.all_objects if store.guid_of(obj))
        box.label(text=f"{count} document object(s)")

        if state.is_stale:
            warning = layout.box()
            warning.alert = True
            warning.label(text="Changed on disk since it was opened.", icon="ERROR")
            warning.label(text="Reload, or your save will be refused.")

        column = layout.column(align=True)
        column.operator("paradise_assets.save_prefab", icon="EXPORT")
        column.operator("paradise_assets.reload_prefab", icon="FILE_REFRESH")
        layout.operator("paradise_assets.open_prefab", text="Open Another…", icon="FILE_FOLDER")


class PARADISE_ASSETS_PT_object(_AssetsPanel, Panel):
    bl_label = "Components"
    bl_idname = "PARADISE_ASSETS_PT_object"
    bl_parent_id = "PARADISE_ASSETS_PT_document"

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def draw(self, context):
        layout = self.layout
        obj = context.active_object

        guid = store.guid_of(obj)
        if guid is None:
            layout.label(text="Not a document object.", icon="DOT")
            return

        layout.label(text=guid, icon="COPY_ID")

        components = store.component_json(obj)
        if not components:
            layout.label(text="No components.")
            return

        # Read-only, and the panel says so once rather than per row: this addon passes component
        # payloads through untouched, so anything typed here would be discarded on save.
        layout.label(text="Read-only — edited in the document.", icon="INFO")

        for component in components:
            box = layout.box()
            box.label(text=_component_label(component), icon="PROPERTIES")
            for line in _payload_lines(component.get("data")):
                box.label(text=line)


def _component_label(component: dict) -> str:
    """The CLR type name where there is one, and the id otherwise.

    The type is the readable half and what an author recognises; the id is the primary key and
    the only thing guaranteed present.
    """
    type_name = component.get("type")
    if isinstance(type_name, str) and type_name:
        return type_name.rsplit(".", 1)[-1]
    return str(component.get("id", "<unknown>"))


def _payload_lines(data, prefix: str = "", depth: int = 0) -> list[str]:
    """A payload flattened to one line per leaf.

    Depth-limited because a panel is not a document viewer: a deeply nested payload would push
    everything else off the screen, and the file is one click away for anyone who needs the rest.
    """
    if not isinstance(data, dict) or depth > 2:
        return []

    lines: list[str] = []
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            lines.extend(_payload_lines(value, f"{path}.", depth + 1))
        elif isinstance(value, list):
            lines.append(f"{path}: [{len(value)} item(s)]")
        else:
            lines.append(f"{path}: {value}")
    return lines


classes = (
    PARADISE_ASSETS_PT_document,
    PARADISE_ASSETS_PT_object,
)
