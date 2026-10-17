"""Add Prefab Instance: what a browser confirm, a multi-selection and a named file each place.

    blender --background --factory-startup --python tests/integration/test_add_prefab.py

What it pins:

- A file browser multi-selection instances EVERY chosen prefab: Blender delivers it as
  ``directory`` + ``files``, and an operator that only listens for ``filepath`` would place
  just the active file and drop the rest (#72).
- A refusal for one file does not refuse the batch: the rest place, and the problems are
  reported. Every file refused means CANCELLED with nothing placed.
- The open document named among the files is refused (a document cannot instantiate itself)
  without taking the rest of the batch with it.
- Only a directly named file skips the browser. ``invoke`` places a named file directly, and
  browse intent -- nothing named -- hands off to the file selector instead of executing: the
  door a REGISTER-restored path used to walk through to re-place the last prefab (#63). The
  panel test pins the button side of that contract (it clears ``filepath``).
- Everything placed survives save and reload as real document entries.

Background Blender has no window to hand the event to, so an INVOKE_DEFAULT call runs
``execute`` directly; ``invoke`` is therefore driven as the plain method it is, against a
stand-in that carries the props a real operator would.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import paradise_assets
from paradise_assets import ops
from paradise_assets.document import prefab, project
from paradise_assets.materialize import load, save, store

failures: list[str] = []


def check(condition: bool, label: str) -> bool:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)
    return condition


#: Distinct identities: the open level, a sibling level in its directory, and two prefabs.
ARENA = "11111111-2222-4333-8444-555555555555"
FOYER = "22222222-3333-4444-8555-666666666666"
CRATE = "66666666-7777-4888-9999-aaaaaaaaaaaa"
BARREL = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"


def _document(guid: str, name: str) -> str:
    return f"""schema_version = 1

[[objects]]

[[objects.components]]
id = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
type = "meta"
Guid = "{guid}"
Name = "{name}"
"""


def make_project(root: str) -> tuple[str, str, str, str]:
    """A minimal asset project: a level to have open, and prefabs to place. Their paths."""
    levels, props = os.path.join(root, "assets", "levels"), os.path.join(root, "assets", "props")
    os.makedirs(levels)
    os.makedirs(props)
    with open(os.path.join(root, "assets", "project.toml"), "w", encoding="utf-8") as handle:
        handle.write('schema_version = 1\nname = "addprefab"\n\n[host]\nproject = "Game/Game.csproj"\n')
    # The schema dump a load insists on (without it every load warns about the suffix fallback);
    # empty is honest here -- nothing in these documents carries a mesh reference.
    os.makedirs(os.path.join(root, ".editor"), exist_ok=True)
    with open(os.path.join(root, ".editor", "authoring-schema.json"), "w", encoding="utf-8") as handle:
        handle.write('{"components": []}\n')
    written = []
    for directory, filename, guid, name in [
        (levels, "arena.prefab", ARENA, "Arena"),
        (levels, "foyer.prefab", FOYER, "Foyer"),
        (props, "crate.prefab", CRATE, "Crate"),
        (props, "barrel.prefab", BARREL, "Barrel"),
    ]:
        with open(os.path.join(directory, filename), "w", encoding="utf-8") as handle:
            handle.write(_document(guid, name))
        # The sidecar add_instance reads identity from; without it a prefab cannot be placed.
        with open(os.path.join(directory, filename + ".meta"), "w", encoding="utf-8") as handle:
            handle.write(f'schema_version = 1\nguid = "{guid}"\n')
        written.append(os.path.join(directory, filename))
    return tuple(written)  # type: ignore[return-value]


def open_level(level: str, layout) -> None:
    """Load the level the way a session would: full materialization, state recorded."""
    result = load.load_document(
        bpy.context.scene, prefab.loads(Path(level).read_text(), level), level, layout)
    assert not result.warnings, result.warnings


class _StandIn:
    """The operator-shaped ``self`` ``invoke`` needs: props, a report channel, the real
    execute. Nothing else in the operator reaches for a real ``bpy.types.Operator``."""

    def __init__(self, filepath: str = "", directory: str = "", files=()) -> None:
        self.filepath = filepath
        self.directory = directory
        self.files = files

    def report(self, severity, message: str) -> None:
        print(f"REPORT {'/'.join(sorted(severity))}: {message}")

    execute = ops.PARADISE_ASSETS_OT_add_prefab_instance.execute
    _selected_paths = ops.PARADISE_ASSETS_OT_add_prefab_instance._selected_paths


def main() -> int:
    paradise_assets.register()
    try:
        with tempfile.TemporaryDirectory() as root:
            level, foyer, crate, barrel = make_project(root)
            layout = project.ProjectLayout(root)
            props = os.path.dirname(crate)
            cursor = (2.0, 3.0, 1.0)

            bpy.ops.wm.read_factory_settings(use_empty=True)
            open_level(level, layout)
            # What the session loaded: everything placed after this is the operator's work.
            session = {store.guid_of(obj) for obj in bpy.data.objects}
            placed: list[str] = []
            bpy.context.scene.cursor.location = cursor

            def placed_since(marker: set) -> list:
                return [obj for obj in bpy.data.objects if store.guid_of(obj) not in marker]

            print("\n== a directly named file (the drag-and-drop shape) ==")
            before = {store.guid_of(obj) for obj in bpy.data.objects}
            check(
                bpy.ops.paradise_assets.add_prefab_instance(filepath=crate) == {"FINISHED"},
                "a named file places without a browser",
            )
            added = placed_since(before)
            check(len(added) == 1, f"exactly one instance ({len(added)})")
            check(
                tuple(added[0].location) == cursor,
                f"placed at the cursor ({tuple(added[0].location)})",
            )
            check(
                added[0].select_get() and bpy.context.selected_objects[:] == added,
                "selected and active after the placement",
            )
            placed += [store.guid_of(obj) for obj in added]

            print("\n== a file browser multi-selection (#72) ==")
            before = {store.guid_of(obj) for obj in bpy.data.objects}
            result = bpy.ops.paradise_assets.add_prefab_instance(
                directory=props, files=[{"name": "crate.prefab"}, {"name": "barrel.prefab"}])
            added = placed_since(before)
            check(
                result == {"FINISHED"} and len(added) == 2,
                f"every file of the selection places ({result}, {len(added)})",
            )
            check(
                {obj.name.split(" ▸")[0].split(".")[0] for obj in added} == {"Crate", "Barrel"},
                f"each under its document's name ({[obj.name for obj in added]})",
            )
            check(
                all(tuple(obj.location) == cursor for obj in added),
                "each at the cursor, like a single placement",
            )
            check(
                all(obj.select_get() for obj in added)
                and set(bpy.context.selected_objects) == set(added),
                "the whole batch selected",
            )
            check(bpy.context.active_object in added, "the last of them active")
            placed += [store.guid_of(obj) for obj in added]

            print("\n== a refused file does not refuse the batch ==")
            before = {store.guid_of(obj) for obj in bpy.data.objects}
            result = bpy.ops.paradise_assets.add_prefab_instance(
                directory=props, files=[{"name": "ghost.prefab"}, {"name": "crate.prefab"}])
            added = placed_since(before)
            check(
                result == {"FINISHED"} and len(added) == 1,
                f"a missing companion skips, the real file still places ({result}, {len(added)})",
            )
            placed += [store.guid_of(obj) for obj in added]

            before = {store.guid_of(obj) for obj in bpy.data.objects}
            try:
                # An ERROR report raises through bpy.ops; that refusal IS the CANCELLED result.
                bpy.ops.paradise_assets.add_prefab_instance(
                    directory=props, files=[{"name": "ghost.prefab"}])
                result = "FINISHED"
            except RuntimeError as error:
                result = str(error).strip()
            added = placed_since(before)
            check(
                result.startswith("Error: no such file: ") and not added,
                f"a batch refused in full places nothing ({result})",
            )

            before = {store.guid_of(obj) for obj in bpy.data.objects}
            result = bpy.ops.paradise_assets.add_prefab_instance(
                directory=os.path.dirname(level),
                files=[{"name": "arena.prefab"}, {"name": os.path.basename(foyer)}])
            added = placed_since(before)
            check(
                result == {"FINISHED"} and len(added) == 1
                and added[0].name.split(" ▸")[0].split(".")[0] == "Foyer",
                "the open document is refused, a sibling in the same selection still places",
            )
            placed += [store.guid_of(obj) for obj in added]

            print("\n== only a named file skips the browser (#63) ==")
            before = {store.guid_of(obj) for obj in bpy.data.objects}
            named = _StandIn(filepath=barrel)
            check(
                ops.PARADISE_ASSETS_OT_add_prefab_instance.invoke(named, bpy.context, None)
                == {"FINISHED"},
                "invoke places a named file directly",
            )
            added = placed_since(before)
            check(len(added) == 1 and tuple(added[0].location) == cursor, "without a browser")

            before = {store.guid_of(obj) for obj in bpy.data.objects}
            browse = _StandIn()  # Nothing named: a click on Add Prefab after the fix.
            try:
                ops.PARADISE_ASSETS_OT_add_prefab_instance.invoke(browse, bpy.context, None)
                browsed = "returned"
            except Exception as error:  # fileselect_add refuses a stand-in in background
                browsed = f"raised {type(error).__name__}"
            check(
                not placed_since(before),
                f"browse intent reaches the file selector, never execute ({browsed})",
            )
            before = {store.guid_of(obj) for obj in bpy.data.objects}
            try:
                bpy.ops.paradise_assets.add_prefab_instance()
                result = "FINISHED"
            except RuntimeError as error:
                result = str(error).strip()
            check(
                result.startswith("Error: No prefab file was chosen.") and not placed_since(before),
                f"an execution with nothing chosen places nothing ({result})",
            )

            print("\n== placed instances are document entries ==")
            guids = [guid for guid in placed if guid not in session]
            save.save_prefab(bpy.context.scene)
            open_level(level, layout)
            check(
                all(store.object_with_guid(bpy.context.scene, guid) is not None for guid in guids),
                f"save and reload restore every placed instance ({len(guids)})",
            )
    finally:
        paradise_assets.unregister()

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
