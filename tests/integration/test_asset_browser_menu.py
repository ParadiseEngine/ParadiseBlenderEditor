"""The Asset Browser context menu, and the sidecar that makes it possible.

    blender --background --factory-startup --python tests/integration/test_asset_browser_menu.py -- <project>

Two things are checked here, and the first is the one that would rot silently.

**The index must be keyed on what the browser will actually show.** A context menu is handed an
``AssetRepresentation`` with no datablock behind it -- ``local_id`` is None -- so the only handle it
has on the asset is its NAME. Blender uniquifies datablock names within a file, so two prefabs both
called "Box" become ``Box`` and ``Box.001``; if ``catalogue.build`` ever keyed the sidecar on the
name the DOCUMENT asked for instead of the name the object ended up with, the menu would keep
working for every prefab with a unique name and quietly stop for exactly the ones that collided.
Nothing would error. The check below builds a colliding pair on purpose.

**Registration must survive a round trip**, because the menu is appended to bundled UI. Getting
that wrong takes the whole addon down -- document opening, panel and drop handler included -- over
a context-menu entry.

Blender cannot open an Asset Browser in background mode, so the click itself is not exercised. What
is exercised is everything the click depends on: which assets the menu offers itself for, and
whether the path it would hand to ``open_prefab`` names a real document.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import paradise_assets  # noqa: E402
from paradise_assets import browser, catalogue  # noqa: E402
from paradise_assets.document import project  # noqa: E402

DEFAULT_PROJECT = r"C:\proj\paradise-workspace\shiningpie"

failures: list[str] = []


def check(condition: bool, label: str) -> bool:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)
    return condition


class FakeAsset:
    """What the browser hands a context menu: a name, a library, a type -- and no datablock."""

    def __init__(self, name: str, library: str, id_type: str = "OBJECT") -> None:
        self.name = name
        self.full_library_path = library
        self.id_type = id_type


def index_of(root: str) -> dict:
    with open(catalogue.index_path(root), encoding="utf-8") as handle:
        return json.load(handle)["assets"]


# --------------------------------------------------------------------------------------


def against_real_project(root: str) -> None:
    layout = project.locate(root)
    if layout is None:
        print(f"SKIP  no asset project at {root}")
        return

    # Previews left ON for the real project, even though nothing here looks at them: this writes
    # over the catalogue the author actually uses, and a test that quietly stripped their
    # thumbnails would be a bad guest. Warm, the renders are skipped anyway.
    made, _, _ = catalogue.build(layout.root)
    index = index_of(layout.root)
    check(len(index) == made, f"the sidecar names all {made} asset(s) ({len(index)} entries)")

    # THE correspondence: every asset the catalogue actually contains is in the index, under the
    # name the browser will show. Read from the built file rather than from the build's own
    # bookkeeping, so this is a statement about the artifact and not about the loop that wrote it.
    bpy.ops.wm.open_mainfile(filepath=catalogue.catalogue_path(layout.root))
    in_blend = {o.name for o in bpy.data.objects if o.asset_data is not None}
    check(in_blend == set(index), f"every asset in the .blend is in the sidecar ({len(in_blend)})")

    missing = [rel["path"] for rel in index.values() if not os.path.isfile(layout.resolve(rel["path"]))]
    check(not missing, f"every indexed path resolves to a document ({missing[:3]})")

    # What the menu would do with a right-click, short of the click.
    library = catalogue.catalogue_path(layout.root)
    name = sorted(index)[0]
    directory = browser._catalogue_directory(FakeAsset(name, library))
    check(directory is not None, f"the menu offers itself for '{name}'")
    if directory is not None:
        check(browser._project_root(directory) == layout.root, "it finds the project from the library path")
        entry = browser._entry(directory, name)
        check(
            entry is not None and os.path.isfile(layout.resolve(entry["path"])),
            f"and resolves '{name}' to {entry['path'] if entry else '<nothing>'}",
        )

    # And what it must decline, so a right-click anywhere else in the browser is untouched.
    declined = {
        "a local asset (no library)": FakeAsset("X", ""),
        "a non-object asset": FakeAsset(name, library, "MATERIAL"),
        "someone else's library": FakeAsset(name, os.path.join(root, "other", "lib.blend")),
    }
    for label, asset in declined.items():
        check(browser._catalogue_directory(asset) is None, f"declines {label}")
    check(browser._catalogue_directory(None) is None, "declines no asset at all")

    check(browser._entry(directory, "NoSuchAssetName") is None, "an unknown name resolves to nothing")


# --------------------------------------------------------------------------------------


PREFAB = """schema_version = 1

[[objects]]

[[objects.components]]
id = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
type = "meta"
Guid = "{guid}"
Name = "Box"
"""

META = 'schema_version = 1\nguid = "{guid}"\nkind = "document"\n'


def colliding_names() -> None:
    """Two prefabs, two folders, one name -- the case a name-keyed sidecar could get wrong."""
    with tempfile.TemporaryDirectory() as root:
        assets = os.path.join(root, "assets")
        for folder in ("props", "set"):
            os.makedirs(os.path.join(assets, folder))
        with open(os.path.join(assets, "project.toml"), "w", encoding="utf-8") as handle:
            handle.write('schema_version = 1\nname = "collide"\n')

        for index, folder in enumerate(("props", "set")):
            guid = f"22222222-3333-4444-8555-00000000000{index}"
            prefab = os.path.join(assets, folder, "box.prefab")
            with open(prefab, "w", encoding="utf-8") as handle:
                handle.write(PREFAB.format(guid=guid))
            with open(prefab + ".meta", "w", encoding="utf-8") as handle:
                handle.write(META.format(guid=guid))

        made, _, _ = catalogue.build(root, previews=False)
        index = index_of(root)
        if not check(made == 2, f"both prefabs entered the catalogue ({made})"):
            return

        check(len(index) == 2, f"both are in the sidecar under distinct names ({sorted(index)})")
        check(
            {entry["path"] for entry in index.values()} == {"props/box.prefab", "set/box.prefab"},
            "and the two names point at DIFFERENT documents",
        )

        # The names must be the ones the .blend actually holds, or the menu looks up a name the
        # browser never shows.
        bpy.ops.wm.open_mainfile(filepath=catalogue.catalogue_path(root))
        in_blend = {o.name for o in bpy.data.objects if o.asset_data is not None}
        check(in_blend == set(index), f"the sidecar's keys are the .blend's names ({sorted(in_blend)})")


def registration() -> None:
    """Appending to bundled UI must be reversible, and must not take the addon down."""
    menu = getattr(bpy.types, browser.MENU, None)
    if not check(menu is not None, f"this Blender has {browser.MENU}"):
        return

    paradise_assets.register()
    check(browser._draw in menu._dyn_ui_initialize(), "the menu entry is appended on register")
    paradise_assets.unregister()
    check(browser._draw not in menu._dyn_ui_initialize(), "and removed again on unregister")

    # Twice, because a reload is the normal way an addon is developed and a leaked append or a
    # double-registered class only shows up on the second pass.
    paradise_assets.register()
    paradise_assets.unregister()
    check(browser._draw not in menu._dyn_ui_initialize(), "a second register/unregister leaves nothing behind")


def main() -> int:
    root = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else DEFAULT_PROJECT

    print("== a real project ==")
    against_real_project(root)
    print("\n== two prefabs with one name ==")
    colliding_names()
    print("\n== registration ==")
    registration()

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
