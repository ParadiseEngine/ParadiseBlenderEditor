"""The right-click entries in the Outliner and the viewport, and the lookup that decides
whether they appear.

    blender --background --factory-startup --python tests/integration/test_context_menu.py

Blender cannot open a context menu in background mode, so the click itself is not exercised.
What is exercised is everything the click depends on, and the part that would rot silently is
the LOOKUP: an instance loaded out of a document carries no prefab reference of its own — the
expansion consumes it — so "open the prefab this came from" only works because
:mod:`materialize.load` tags the object as it materializes. Nothing else in the addon reads that
tag for an existing object, so a regression there breaks this menu and nothing else.

The second entry is the extract operator the sidebar already has; what is checked is that the
menu names an operator that exists and that its poll agrees about when it is offerable.

Registration must survive a round trip, because the entries are appended to bundled UI. Getting
that wrong takes the whole addon down — document opening, panel and drop handler included — over
a context-menu entry.
"""

from __future__ import annotations

import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import paradise_assets
from paradise_assets import context_menu as menus
from paradise_assets.document import prefab as prefab_document
from paradise_assets.document import project
from paradise_assets.materialize import load, store

failures: list[str] = []


def check(condition: bool, label: str) -> bool:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)
    return condition


PROP_GUID = "11111111-2222-4333-8444-555555555555"
PROP = f"""schema_version = 1

[[objects]]

[[objects.components]]
id = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
type = "meta"
Guid = "{PROP_GUID}"
Name = "Crate"
"""

LEVEL = f"""schema_version = 1

[[objects]]

[[objects.components]]
id = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
type = "meta"
Guid = "aaaaaaaa-2222-4333-8444-555555555555"
Name = "Level"

[[objects]]
prefab = {{ guid = "{PROP_GUID}", path = "props/crate.prefab" }}

[[objects.components]]
id = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
type = "meta"
Guid = "bbbbbbbb-2222-4333-8444-555555555555"
Name = "Crate"
Parent = "aaaaaaaa-2222-4333-8444-555555555555"
"""


def make_project(root: str) -> tuple[str, str]:
    """A level that instances one prop. Returns (level path, prop path)."""
    os.makedirs(os.path.join(root, "assets", "levels"))
    os.makedirs(os.path.join(root, "assets", "props"))
    with open(os.path.join(root, "assets", "project.toml"), "w", encoding="utf-8") as handle:
        handle.write('schema_version = 1\nname = "outlinertest"\n')

    prop = os.path.join(root, "assets", "props", "crate.prefab")
    with open(prop, "w", encoding="utf-8") as handle:
        handle.write(PROP)
    # The prop needs an identity for the reference to resolve to it.
    with open(prop + ".meta", "w", encoding="utf-8") as handle:
        handle.write(f'schema_version = 1\nguid = "{PROP_GUID}"\n')

    level = os.path.join(root, "assets", "levels", "arena.prefab")
    with open(level, "w", encoding="utf-8") as handle:
        handle.write(LEVEL)
    with open(level + ".meta", "w", encoding="utf-8") as handle:
        handle.write('schema_version = 1\nguid = "cccccccc-2222-4333-8444-555555555555"\n')
    return level, prop


def open_level(level: str, layout) -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with open(level, encoding="utf-8") as handle:
        document = prefab_document.loads(handle.read(), level)
    load.load_document(bpy.context.scene, document, level, layout)


class Props:
    """What ``layout.operator`` hands back."""


class Layout:
    """A recording stand-in for ``UILayout``; every sub-layout records into the same run."""

    def __init__(self, run=None) -> None:
        self.run = run if run is not None else []
        self.operator_context = ""

    def separator(self, **_kwargs) -> None:
        pass

    def operator(self, idname, **_kwargs) -> Props:
        self.run.append(idname)
        return Props()

    def column(self, **_kwargs):
        return Layout(self.run)


class Host:
    """Stands in for the Menu instance; ``_draw`` only reaches for ``self.layout``."""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout


def drawn(context) -> list:
    """The operator ids ``_draw`` would put in the menu for this context."""
    layout = Layout()
    menus._draw(Host(layout), context)
    return layout.run


def named(name: str):
    return next((obj for obj in bpy.context.scene.collection.all_objects if obj.name == name), None)


def main() -> int:
    paradise_assets.register()
    try:
        with tempfile.TemporaryDirectory() as root:
            level, prop = make_project(root)
            layout = project.locate(level)
            open_level(level, layout)

            print("\n== an instance loaded out of a document knows its prefab ==")
            instance = named("Crate")
            if check(instance is not None, "the instance materialized"):
                check(
                    store.prefab_of(instance) == (PROP_GUID, "props/crate.prefab"),
                    f"and carries its prefab reference ({store.prefab_of(instance)})",
                )
                check(
                    menus.prefab_of(instance) is not None,
                    "so the menu entry has something to open",
                )

            print("\n== the level root does not ==")
            root_object = named("Level")
            check(
                root_object is not None and menus.prefab_of(root_object) is None,
                "a plain document object instantiates nothing",
            )

            print("\n== the lookup walks up to the instance ==")
            child = bpy.data.objects.new("LooseChild", None)
            bpy.context.scene.collection.objects.link(child)
            child.parent = instance
            check(
                menus.prefab_of(child) == (PROP_GUID, "props/crate.prefab"),
                "a child of an instance answers with the instance's prefab",
            )
            child.parent = root_object
            check(
                menus.prefab_of(child) is None,
                "and one under a plain object answers with nothing",
            )

            print("\n== what the entries would run ==")
            for idname in ("open_prefab_elsewhere", "extract_prefab", "group_objects"):
                check(
                    hasattr(bpy.ops.paradise_assets, idname),
                    f"paradise_assets.{idname} exists",
                )

            bpy.context.view_layer.objects.active = instance
            check(
                menus.PARADISE_ASSETS_OT_open_prefab_elsewhere.poll(bpy.context),
                "Open Prefab polls true on the instance",
            )
            bpy.context.view_layer.objects.active = root_object
            check(
                not menus.PARADISE_ASSETS_OT_open_prefab_elsewhere.poll(bpy.context),
                "and false on an object that instantiates nothing",
            )

            print("\n== the new Blender is started on the prefab, not on this session's file ==")
            recorded: list = []

            class FakePopen:
                def __init__(self, argv, **kwargs):
                    recorded.append((argv, kwargs))

            original = menus.subprocess.Popen
            menus.subprocess.Popen = FakePopen
            try:
                bpy.context.view_layer.objects.active = instance
                bpy.ops.paradise_assets.open_prefab_elsewhere()
            finally:
                menus.subprocess.Popen = original

            if check(len(recorded) == 1, f"one Blender started ({len(recorded)})"):
                argv, kwargs = recorded[0]
                check(argv[0] == bpy.app.binary_path, "it is Blender")
                check(
                    argv[-1] == prop and argv[-2] == "--",
                    f"opening the PROP, passed after -- ({argv[-1]})",
                )
                check(
                    "--factory-startup" not in argv,
                    "with the user's preferences, or the addon would not be enabled there",
                )
                check(
                    kwargs.get("cwd") == layout.root,
                    f"in the project root ({kwargs.get('cwd')})",
                )
                check(
                    kwargs.get("start_new_session") == (os.name != "nt"),
                    "detached, so it outlives this Blender",
                )

            print("\n== a reference to a document that is gone is refused, not launched ==")
            os.remove(prop)
            recorded.clear()
            menus.subprocess.Popen = FakePopen
            # bpy.ops RAISES on an operator that reports {"ERROR"}; the refusal is the exception.
            refused = None
            try:
                bpy.ops.paradise_assets.open_prefab_elsewhere()
            except RuntimeError as error:
                refused = str(error)
            finally:
                menus.subprocess.Popen = original
            check(
                refused is not None and "not on disk" in refused,
                f"the operator refused and said why ({refused})",
            )
            check(not recorded, "and started nothing")

            print("\n== the menu offers itself only for document objects ==")
            bpy.context.view_layer.objects.active = instance
            check(
                drawn(bpy.context) == [
                    "paradise_assets.open_prefab_elsewhere",
                    "paradise_assets.extract_prefab",
                    "paradise_assets.group_objects",
                ],
                f"all three entries on a document object ({drawn(bpy.context)})",
            )
            outsider = bpy.data.objects.new("JustACube", None)
            bpy.context.scene.collection.objects.link(outsider)
            bpy.context.view_layer.objects.active = outsider
            check(
                drawn(bpy.context) == [],
                "and none on an object the document does not own",
            )

            print("\n== registration, in BOTH menus ==")
            check(
                menus.MENUS == ("OUTLINER_MT_object", "VIEW3D_MT_object_context_menu"),
                f"the Outliner's and the viewport's ({menus.MENUS})",
            )
            for name in menus.MENUS:
                menu = getattr(bpy.types, name, None)
                if not check(menu is not None, f"this Blender has {name}"):
                    continue
                check(menus._draw in menu._dyn_ui_initialize(), f"{name}: the entries are appended")

            menus.unregister_menu()
            for name in menus.MENUS:
                menu = getattr(bpy.types, name, None)
                if menu is not None:
                    check(
                        menus._draw not in menu._dyn_ui_initialize(),
                        f"{name}: and removed again on unregister",
                    )
            menus.register_menu()
    finally:
        paradise_assets.unregister()

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
