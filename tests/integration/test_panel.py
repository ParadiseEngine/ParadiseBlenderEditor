"""The sidebar panels: what each one offers, and when.

    blender --background --factory-startup --python tests/integration/test_panel.py

A panel is the one part of an addon with no other test: its ``draw`` runs only when Blender has
a 3D viewport, which a background run does not, so a typo in an operator id or an attribute
that only exists once a document is open ships and is found by an author. The panels are plain
Python classes, though, so ``draw`` can be called against a recording stand-in for
``UILayout`` -- which catches exactly those two failures, and pins the polls besides.

What it pins:

- The PROJECT panel is available with no document open. That is the whole point of the split
  (#35): the watcher and Build belong to a project, and a session that has just started has no
  document but does have a project.
- Play and Components are NOT, because both need a document.
- Every operator any panel draws exists.
- The landing state offers the project's documents, and offers them BY PATH -- a row that did
  not set ``filepath`` would silently open the file browser instead.
- Every panel is TOP-LEVEL. A ``bl_parent_id`` naming a panel that is not registered makes
  Blender drop the child silently, so the registration order and the parent set are checked.
"""

from __future__ import annotations

import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import paradise_assets
from paradise_assets import ui
from paradise_assets.materialize import store

failures: list[str] = []


def check(condition: bool, label: str) -> bool:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)
    return condition


PREFAB = """schema_version = 1

[[objects]]

[[objects.components]]
id = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
type = "meta"
Guid = "33333333-4444-4555-8666-777777777777"
Name = "Level"
"""


class Props:
    """What ``layout.operator`` hands back: anything may be assigned to it."""


class Layout:
    """A recording stand-in for ``UILayout``. Every sub-layout records into the same run, so
    ``operators`` is everything the panel offered however deeply it nested them."""

    def __init__(self, run=None) -> None:
        self.run = run if run is not None else {"operators": [], "labels": [], "props": []}
        self.alert = False

    @property
    def operators(self) -> list[str]:
        return self.run["operators"]

    @property
    def labels(self) -> list[str]:
        return self.run["labels"]

    def label(self, text="", icon="") -> None:
        self.run["labels"].append(text)

    def operator(self, idname, text="", icon="", **_kwargs) -> Props:
        properties = Props()
        self.run["operators"].append(idname)
        self.run["props"].append((idname, properties))
        return properties

    def prop(self, *_args, **_kwargs) -> None:
        pass

    def separator(self, **_kwargs) -> None:
        pass

    def box(self):
        return Layout(self.run)

    def row(self, **_kwargs):
        return Layout(self.run)

    def column(self, **_kwargs):
        return Layout(self.run)


class Host:
    """Stands in for the Panel instance. A REGISTERED panel is a ``bpy_struct`` and cannot be
    constructed from Python, but ``draw`` only ever reaches for ``self.layout``."""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout


def draw(panel_class, context) -> Layout:
    """Run one panel's ``draw`` against the stand-in and return what it drew."""
    layout = Layout()
    panel_class.draw(Host(layout), context)
    return layout


def properties_for(layout: Layout, idname: str) -> list:
    return [props for name, props in layout.run["props"] if name == idname]


def make_project(root: str) -> str:
    """A minimal asset project with one document. Returns the document path."""
    assets = os.path.join(root, "assets", "levels")
    os.makedirs(assets)
    with open(os.path.join(root, "assets", "project.toml"), "w", encoding="utf-8") as handle:
        handle.write('schema_version = 1\nname = "paneltest"\n\n[host]\nproject = "Game/Game.csproj"\n')
    # The sidecar the landing state's listing needs: `list_assets` only returns identified files.
    with open(os.path.join(assets, "arena.prefab.meta"), "w", encoding="utf-8") as handle:
        handle.write('schema_version = 1\nguid = "33333333-4444-4555-8666-777777777777"\n')

    document = os.path.join(assets, "arena.prefab")
    with open(document, "w", encoding="utf-8") as handle:
        handle.write(PREFAB)
    return document


def main() -> int:
    paradise_assets.register()
    try:
        with tempfile.TemporaryDirectory() as root:
            document = make_project(root)
            context = bpy.context

            print("\n== no document open, but the .blend is inside a project ==")
            bpy.ops.wm.read_factory_settings(use_empty=True)
            workfile = os.path.join(root, ".editor", "blend", "scratch.blend")
            os.makedirs(os.path.dirname(workfile), exist_ok=True)
            bpy.ops.wm.save_as_mainfile(filepath=workfile)
            ui._listings.clear()

            check(store.read_state(context.scene) is None, "no document is open")
            check(
                store.project_of(context.scene) is not None,
                "the project is found from the .blend alone",
            )
            check(
                ui.PARADISE_ASSETS_PT_project.poll(context),
                "Project is available with no document open",
            )
            check(
                not ui.PARADISE_ASSETS_PT_play.poll(context),
                "Play is not: it runs the game on a document",
            )
            check(
                not ui.PARADISE_ASSETS_PT_object.poll(context),
                "Components is not: it edits a document object",
            )

            landing = draw(ui.PARADISE_ASSETS_PT_document, context)
            offered = [
                props.filepath for props in properties_for(landing, "paradise_assets.open_prefab")
                if hasattr(props, "filepath")
            ]
            check(
                offered == [document],
                f"the landing state offers the project's document by path ({offered})",
            )

            project_panel = draw(ui.PARADISE_ASSETS_PT_project, context)
            check(
                "paradise_assets.toggle_watch" in project_panel.operators
                and "paradise_assets.build" in project_panel.operators,
                "the watcher and Build are offered with no document open",
            )

            print("\n== a document open ==")
            store.write_state(context.scene, document)
            bpy.ops.mesh.primitive_cube_add(size=1.0)
            ui._listings.clear()

            check(ui.PARADISE_ASSETS_PT_play.poll(context), "Play is available")
            check(
                ui.PARADISE_ASSETS_PT_object.poll(context),
                "Components is available with an object selected",
            )

            open_panel = draw(ui.PARADISE_ASSETS_PT_document, context)
            check(
                "arena.prefab" in open_panel.labels,
                f"the document panel names the open document ({open_panel.labels[:2]})",
            )
            check(
                "paradise_assets.save_prefab" in open_panel.operators,
                "and offers Save",
            )
            check(
                not any(
                    hasattr(props, "filepath")
                    for props in properties_for(open_panel, "paradise_assets.open_prefab")
                ),
                "while Open Another… browses rather than naming a file",
            )

            components = draw(ui.PARADISE_ASSETS_PT_object, context)
            check(
                components.labels == ["Not a document object."],
                f"Components says so for an object the document does not own ({components.labels})",
            )

            print("\n== the panel set ==")
            check(
                [cls.bl_idname for cls in ui.classes] == [
                    "PARADISE_ASSETS_PT_document",
                    "PARADISE_ASSETS_PT_project",
                    "PARADISE_ASSETS_PT_play",
                    "PARADISE_ASSETS_PT_object",
                ],
                "four panels, in the order they read down the sidebar",
            )
            check(
                not any(getattr(cls, "bl_parent_id", "") for cls in ui.classes),
                "all top-level: a bl_parent_id naming an unregistered panel is dropped silently",
            )

            print("\n== every operator any panel draws exists ==")
            drawn = set()
            for panel_class in ui.classes:
                if getattr(panel_class, "poll", None) is None or panel_class.poll(context):
                    drawn.update(draw(panel_class, context).operators)
            drawn.update(landing.operators)
            drawn.update(project_panel.operators)

            missing = sorted(
                idname for idname in drawn
                if not hasattr(getattr(bpy.ops, idname.split(".")[0]), idname.split(".")[1])
            )
            check(not missing, f"{len(drawn)} operator(s) drawn, none missing ({missing})")
    finally:
        paradise_assets.unregister()

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
