"""Creating prefabs from inside Blender: extraction, and the model prefab mirror.

    blender --background --factory-startup --python tests/integration/test_create_prefab.py -- <project>

``<project>`` is a directory holding ``assets/project.toml``; it defaults to the workspace's
ShiningPie checkout. **The checkout is never written to.** Everything here works on a COPY of
``assets/`` plus the schema dump, made in a temp directory -- these operators create and delete
files, and a test that ran against real content and got one rule wrong would be a diff nobody
asked for at best.

The addon mints no identities: it writes a document and waits for ``paradise assets watch`` to
give it one. So these checks run a REAL watcher against the temp project -- the operators start
it themselves -- rather than simulating the mint. That is the only way the wait is proved to end
against the tool that actually ends it.

THE test is that an extraction changes nothing you can see: the same objects stand in the same
places afterwards, because the instance keeps the extracted object's identity and its placement
while the prefab holds the shape. The mirror's test is the opposite one -- it must NOT touch a
hand-authored prefab, and must not delete anything until a model has really gone.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import uuid

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import addon_utils

from paradise_assets import model_watch, watch
from paradise_assets.document import (
    model_prefabs,
    project,
    schema,
    sidecar,
    well_known,
)
from paradise_assets.document import prefab as prefab_document
from paradise_assets.materialize import load, save, store
from paradise_assets.play import host

DEFAULT_PROJECT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "ShiningPie",
)

STATIC_COMPONENT = "ShiningPie.Authoring.ObstacleMesh"
SKINNED_COMPONENT = "ShiningPie.Authoring.SkinnedMesh"

#: ShiningPie's only rigged models, by GLB stem. Everything else is static.
RIGGED = {"Enemy", "Player"}

failures: list[str] = []


def check(condition: bool, label: str) -> bool:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)
    return condition


def fresh_scene() -> bpy.types.Scene:
    """An empty scene WITHOUT `wm.read_factory_settings`: that resets the preferences, which
    disables the addon, and every `bpy.ops.paradise_assets.*` call after it fails with "could
    not be found". This test drives the real operators, so it clears objects by hand."""
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    return bpy.context.scene


def copy_project(source: str, destination: str) -> str:
    """``assets/`` and the schema dump only -- nothing this test writes can reach the checkout."""
    os.makedirs(destination, exist_ok=True)
    shutil.copytree(os.path.join(source, "assets"), os.path.join(destination, "assets"))
    for candidate in project.SCHEMA_CANDIDATES:
        found = os.path.join(source, candidate.replace("/", os.sep))
        if os.path.isfile(found):
            target = os.path.join(destination, candidate.replace("/", os.sep))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(found, target)
            break
    return destination


@contextlib.contextmanager
def watching():
    """Stop every `paradise assets watch` this section started before its directory goes away.

    The operators start one themselves -- a new prefab has no identity until the watcher mints
    its sidecar -- and a watcher left running against a deleted temp tree writes into nothing and
    logs about it for the rest of the session.
    """
    try:
        yield
    finally:
        watch.stop_all()


def check_watcher() -> bool:
    """Whether a watcher can run at all here. Without one nothing can be created, so the
    creating half of this test would report a real failure for a missing tool."""
    if host.resolve_cli_command() is not None:
        return True
    print("SKIP: no `paradise` CLI resolved, so no watcher can mint identities")
    return False


def open_document(path: str, layout) -> load.LoadResult:
    with open(path, encoding="utf-8") as handle:
        document = prefab_document.loads(handle.read(), path)
    return load.load_document(fresh_scene(), document, path, layout)


def placements(scene) -> dict[str, tuple]:
    """Where every document object stands, keyed by identity."""
    bpy.context.view_layer.update()
    return {
        store.guid_of(obj): tuple(round(v, 6) for row in obj.matrix_world for v in row)
        for obj in scene.collection.all_objects
        if store.guid_of(obj) is not None
    }


def run_cli(verb: list[str], root: str) -> tuple[int, str] | None:
    """A ``paradise`` run against the temp project, or ``None`` when the CLI does not resolve."""
    command = host.resolve_cli_command()
    if command is None:
        return None
    try:
        completed = subprocess.run(
            [*command, *verb, "--project", root],
            cwd=root, capture_output=True, text=True, timeout=600,
            env=host.subprocess_environment(),
        )
    except (OSError, subprocess.SubprocessError) as error:
        return -1, str(error)
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def test_registration() -> None:
    """The operators the panel draws must exist, or the buttons are dead."""
    print("\n== the addon registers, with the two new operators ==")
    # Against bpy.types: `bpy.ops.<x>.<y>` hands back a callable stub whatever the name, so
    # hasattr on it would pass for an operator that does not exist.
    for name in ("EXTRACT_PREFAB", "MIRROR_MODEL_PREFABS", "SET_MESH_COMPONENT"):
        check(
            hasattr(bpy.types, f"PARADISE_ASSETS_OT_{name.lower()}"),
            f"paradise_assets.{name.lower()} is registered",
        )

    preferences = bpy.context.preferences.addons["paradise_assets"].preferences
    # The mirror deletes files, so an addon update must never turn it on under an author.
    check(preferences.mirror_model_prefabs is False, "the model prefab mirror ships off")


def test_extraction(source: str) -> None:
    print("\n== extracting a subtree leaves the level looking identical ==")
    with tempfile.TemporaryDirectory() as work, watching() as _stop:
        root = copy_project(source, os.path.join(work, "project"))
        layout = project.locate(root)
        level = layout.resolve("levels/test.prefab")

        open_document(level, layout)
        before = placements(bpy.context.scene)
        subject = store.object_with_guid(bpy.context.scene, _named(level, "BoulderScaled"))
        check(subject is not None, "the object to extract is in the scene")
        bpy.context.view_layer.objects.active = subject

        target = layout.resolve("prefabs/boulder.prefab")
        result = bpy.ops.paradise_assets.extract_prefab(filepath=target)
        check(result == {"FINISHED"}, f"the extract operator finished ({result})")

        check(os.path.isfile(target), "the prefab was written")
        check(os.path.isfile(sidecar.path_for(target)), "its sidecar was written")

        after = placements(bpy.context.scene)
        check(set(before) == set(after), f"the same identities are present ({len(before)} -> {len(after)})")
        moved = [guid for guid in before if before.get(guid) != after.get(guid)]
        check(moved == [], f"nothing moved ({len(moved)} object(s) did)")

        with open(level, encoding="utf-8") as handle:
            remaining = prefab_document.loads(handle.read(), level)
        instance = remaining.by_guid()[_named(level, "BoulderScaled", cached=True)]
        check(instance.prefab is not None, "the level now holds an instance of the new prefab")
        check(
            [c.id for c in instance.components] == [well_known.META_ID, well_known.TRANSFORM_ID],
            "the instance carries meta and transform only, so the prefab is not shadowed",
        )

        with open(target, "rb") as handle:
            original = handle.read()
        open_document(target, layout)
        save.save_prefab(bpy.context.scene)
        with open(target, "rb") as handle:
            check(handle.read() == original, "the new prefab round-trips byte for byte")

        print("\n== extraction refuses what it cannot do ==")
        open_document(level, layout)
        document_root = store.object_with_guid(bpy.context.scene, remaining.root_guid)
        bpy.context.view_layer.objects.active = document_root
        check(
            not bpy.ops.paradise_assets.extract_prefab.poll(),
            "extracting the document root is refused by the poll",
        )

        bpy.context.view_layer.objects.active = store.object_with_guid(
            bpy.context.scene, _named(level, "Player"))
        # An operator that reports ERROR and cancels surfaces as a RuntimeError through bpy.ops.
        try:
            bpy.ops.paradise_assets.extract_prefab(filepath=target)
            check(False, "extracting onto an existing prefab is refused")
        except RuntimeError as error:
            check("already exists" in str(error), f"extracting onto an existing prefab is refused: {error}")

        report = run_cli(["assets", "prefab-check"], root)
        if report is None:
            print("SKIP: no `paradise` CLI resolved, so prefab-check was not run")
        else:
            check(report[0] == 0, f"prefab-check passes on the extracted tree: {report[1][-300:]}")


_NAMES: dict[str, str] = {}


def _named(level: str, name: str, cached: bool = False) -> str:
    """The identity of the level object called *name*, read once from the file."""
    if not cached or name not in _NAMES:
        with open(level, encoding="utf-8") as handle:
            document = prefab_document.loads(handle.read(), level)
        for entry in document.objects:
            if entry.name is not None and entry.guid is not None:
                _NAMES.setdefault(entry.name, entry.guid)
    return _NAMES[name]


LEVEL_GUIDS = {
    "Level": "aaaaaaaa-0000-4000-8000-000000000001",
    "Rack": "aaaaaaaa-0000-4000-8000-000000000002",
    "Shelf": "aaaaaaaa-0000-4000-8000-000000000003",
    "Cup": "aaaaaaaa-0000-4000-8000-000000000004",
    "Lamp": "aaaaaaaa-0000-4000-8000-000000000005",
}


def _probe_level(work: str) -> str:
    """A project with a three-deep subtree. ShiningPie's levels are flat, and the placement of a
    CHILD is exactly what an extraction can get wrong: the prefab root moves to the origin, so a
    grandchild that came back in the wrong frame is the failure this catches."""
    assets = os.path.join(work, "assets", "levels")
    os.makedirs(assets)
    with open(os.path.join(work, "assets", "project.toml"), "w", encoding="utf-8", newline="") as handle:
        handle.write('name = "probe"\nschema_version = 1\n')

    def meta(body: str) -> str:
        return f'\n[[objects.components]]\nid = "{well_known.META_ID}"\ntype = "meta"\n' + body

    def transform(position, rotation=(0.0, 0.0, 0.0, 1.0), scale=(1.0, 1.0, 1.0)) -> str:
        return (
            f'\n[[objects.components]]\nid = "{well_known.TRANSFORM_ID}"\ntype = "transform"\n'
            f"Position = {list(position)}\nRotation = {list(rotation)}\nScale = {list(scale)}\n"
        )

    turn = (0.0, 0.7071067811865476, 0.0, 0.7071067811865476)
    text = (
        "schema_version = 1\n\n[[objects]]\n"
        + meta(f'Guid = "{LEVEL_GUIDS["Level"]}"\nName = "Level"\n') + transform((0.0, 0.0, 0.0))
        + "\n[[objects]]\n"
        + meta(f'Guid = "{LEVEL_GUIDS["Rack"]}"\nName = "Rack"\nParent = "{LEVEL_GUIDS["Level"]}"\n')
        + transform((2.0, 0.0, -3.0), turn, (1.0, 2.0, 1.0))
        + "\n[[objects]]\n"
        + meta(f'Guid = "{LEVEL_GUIDS["Shelf"]}"\nName = "Shelf"\nParent = "{LEVEL_GUIDS["Rack"]}"\n')
        + transform((0.0, 1.5, 0.0))
        + "\n[[objects]]\n"
        + meta(f'Guid = "{LEVEL_GUIDS["Cup"]}"\nName = "Cup"\nParent = "{LEVEL_GUIDS["Shelf"]}"\n')
        + transform((0.25, 0.0, 0.5), turn)
        + "\n[[objects]]\n"
        + meta(f'Guid = "{LEVEL_GUIDS["Lamp"]}"\nName = "Lamp"\nParent = "{LEVEL_GUIDS["Level"]}"\n')
        + transform((-1.0, 0.0, 0.0))
    )
    path = os.path.join(assets, "probe.prefab")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


def test_nested_extraction() -> None:
    print("\n== a three-deep subtree comes back in the same places ==")
    with tempfile.TemporaryDirectory() as work, watching() as _stop:
        level = _probe_level(work)
        layout = project.locate(level)

        open_document(level, layout)
        before = _by_name(bpy.context.scene)
        check(len(before) == 5, f"the probe level materializes 5 objects ({len(before)})")

        bpy.context.view_layer.objects.active = store.object_with_guid(
            bpy.context.scene, LEVEL_GUIDS["Rack"])
        result = bpy.ops.paradise_assets.extract_prefab(
            filepath=layout.resolve("prefabs/rack.prefab"))
        check(result == {"FINISHED"}, f"the extract operator finished ({result})")

        after = _by_name(bpy.context.scene)
        check(set(before) == set(after), f"the same objects are shown ({sorted(set(before) ^ set(after))})")
        moved = [name for name in before if before[name] != after.get(name)]
        check(moved == [], f"no object moved ({moved})")

        derived = [
            obj for obj in bpy.context.scene.collection.all_objects if store.is_derived(obj)
        ]
        check(
            sorted(o.name for o in derived) == ["Cup", "Shelf"],
            f"the prefab's children are shown as derived ({sorted(o.name for o in derived)})",
        )

        with open(level, encoding="utf-8") as handle:
            remaining = prefab_document.loads(handle.read(), level)
        check(
            [o.name for o in remaining.objects] == ["Level", "Rack", "Lamp"],
            f"the level holds three objects now ({[o.name for o in remaining.objects]})",
        )

        with open(layout.resolve("prefabs/rack.prefab"), encoding="utf-8") as handle:
            extracted = prefab_document.loads(handle.read(), "rack.prefab")
        root = extracted.root()
        check(root.guid != LEVEL_GUIDS["Rack"], "the prefab root has a prefab-local identity")
        check(
            root.component(well_known.TRANSFORM_ID).data["Position"] == [0.0, 0.0, 0.0],
            "and sits at the origin, because placement belongs to the instance",
        )


def _by_name(scene) -> dict[str, tuple]:
    """Where every document object stands, keyed by name: a resolved child's identity is minted
    per instance, so it cannot be the key across an extraction."""
    bpy.context.view_layer.update()
    return {
        obj.name: tuple(round(v, 5) for row in obj.matrix_world for v in row)
        for obj in scene.collection.all_objects
        if store.guid_of(obj) is not None
    }


def test_mirror(source: str) -> None:
    print("\n== the mirror generates one prefab per model ==")
    with tempfile.TemporaryDirectory() as work, watching() as _stop:
        root = copy_project(source, os.path.join(work, "project"))
        layout = project.locate(root)
        preferences = bpy.context.preferences.addons["paradise_assets"].preferences
        preferences.static_mesh_component = STATIC_COMPONENT
        preferences.skinned_mesh_component = SKINNED_COMPONENT

        open_document(layout.resolve("levels/test.prefab"), layout)

        models = _models(layout)
        check(bool(models), f"{len(models)} model(s) in the copied project")

        rigged_models = sorted(m.stem for m in models if m.skinned)
        check(
            set(rigged_models) == RIGGED,
            f"the rigged models are found by reading the GLBs ({rigged_models})",
        )

        check(bpy.ops.paradise_assets.mirror_model_prefabs() == {"FINISHED"}, "the mirror ran")
        generated = model_prefabs.read_generated(layout, schema.load(layout.root))
        check(
            len(generated) == len(models),
            f"one generated prefab per model ({len(generated)} of {len(models)})",
        )
        check(
            {p.model_guid for p in generated} == {m.guid for m in models},
            "each generated prefab names its model by identity",
        )
        check(
            all(p.mesh_path == _model_path(models, p.model_guid) for p in generated),
            "each mesh reference points at where its model lives",
        )
        cube = next(p for p in generated if p.stem == "Prim_Cube")
        check(
            cube.relative == "prefabs/models/Prim_Cube.prefab",
            f"a generated prefab is filed under prefabs/models ({cube.relative})",
        )

        rigged = {p.stem for p in generated if p.mesh_component_id == _component_id(
            layout, SKINNED_COMPONENT)}
        check(rigged == RIGGED, f"and only the rigged ones get the skinned component ({sorted(rigged)})")
        static = {p.stem for p in generated if p.mesh_component_id == _component_id(
            layout, STATIC_COMPONENT)}
        check(
            static == {m.stem for m in models} - RIGGED,
            f"the rest get the static one ({len(static)})",
        )

        check(
            all(_marker(p) == p.model_guid for p in generated),
            "the marker is meta.GeneratedFrom on the root, not a sidecar settings domain",
        )
        check(
            all(sidecar.read(sidecar.path_for(p.path)).settings == {} for p in generated),
            "so the sidecars carry nothing but the watcher's identity",
        )

        print("\n== a second pass changes nothing, and hand-authored prefabs are untouched ==")
        hand = layout.resolve("prefabs/box.prefab")
        with open(hand, "rb") as handle:
            before = handle.read()
        bpy.ops.paradise_assets.mirror_model_prefabs()
        check(
            len(model_prefabs.read_generated(layout, schema.load(layout.root))) == len(models),
            "the second pass generated nothing new",
        )
        with open(hand, "rb") as handle:
            check(handle.read() == before, "the hand-authored box.prefab was not touched")
        with open(hand, encoding="utf-8") as handle:
            hand_root = prefab_document.loads(handle.read(), hand).root()
        check(
            model_prefabs.GENERATED_FROM not in hand_root.meta.data,
            "and it is still not marked generated",
        )

        print("\n== the CLI accepts everything the mirror wrote ==")
        # Before the rename and delete cases below, which deliberately leave dangling models
        # behind: those are this test's damage, not the mirror's output.
        for verb in (["assets", "prefab-check"], ["assets", "verify"]):
            report = run_cli(verb, root)
            if report is None:
                print(f"SKIP: no `paradise` CLI resolved, so `{' '.join(verb)}` was not run")
                continue
            check(report[0] == 0, f"`paradise {' '.join(verb)}` exits 0: {report[1][-400:]}")

        print("\n== a renamed model takes its prefab's reference with it ==")
        renamed = layout.resolve("Models/Boulder.glb")
        os.replace(layout.resolve("Models/Prim_Sphere.glb"), renamed)
        os.replace(
            sidecar.path_for(layout.resolve("Models/Prim_Sphere.glb")), sidecar.path_for(renamed)
        )
        bpy.ops.paradise_assets.mirror_model_prefabs()

        after = model_prefabs.read_generated(layout, schema.load(layout.root))
        sphere = next(p for p in after if p.model_guid == _guid_of(models, "Models/Prim_Sphere.glb"))
        check(sphere.mesh_path == "Models/Boulder.glb", f"the mesh path followed ({sphere.mesh_path})")
        check(
            sphere.relative == "prefabs/models/Prim_Sphere.prefab",
            f"and the prefab kept its name, so levels still point at it ({sphere.relative})",
        )
        check(
            len(after) == len(generated),
            f"nothing was generated or deleted for the rename ({len(after)})",
        )

        print("\n== a deleted model waits out the grace period before its prefab goes ==")
        doomed = layout.resolve("Models/Skyline_1.glb")
        doomed_guid = _guid_of(models, "Models/Skyline_1.glb")
        os.remove(doomed)
        os.remove(sidecar.path_for(doomed))
        prefab_path = layout.resolve("prefabs/models/Skyline_1.prefab")

        bpy.ops.paradise_assets.mirror_model_prefabs()
        check(os.path.isfile(prefab_path), "the first pass deletes nothing")
        bpy.ops.paradise_assets.mirror_model_prefabs()
        check(
            os.path.isfile(prefab_path),
            "nor the second, because the grace period has not elapsed (a Finder move looks "
            "exactly like this for a moment)",
        )

        # Backdated rather than slept through: the two fences are a poll COUNT and an elapsed
        # time, and this leaves the count fence doing its own work above.
        absences = model_watch._ABSENCES[layout.root]
        absences[doomed_guid] = model_prefabs.Absence(absences[doomed_guid].polls, -60.0)
        bpy.ops.paradise_assets.mirror_model_prefabs()
        check(not os.path.exists(prefab_path), "once the model has really gone, so does its prefab")
        check(not os.path.exists(sidecar.path_for(prefab_path)), "and its sidecar with it")

        print("\n== a prefab a level instantiates is never deleted ==")
        # Re-listed: the rename and the delete above moved things, so the opening `models` list
        # no longer says where everything lives.
        current = _models(layout)
        kept = next(
            p for p in model_prefabs.read_generated(layout, schema.load(layout.root))
            if p.stem == "Prim_Cube"
        )
        _instantiate(layout, kept)
        model = _model_path(current, kept.model_guid)
        os.remove(layout.resolve(model))
        os.remove(sidecar.path_for(layout.resolve(model)))
        for _ in range(2):
            bpy.ops.paradise_assets.mirror_model_prefabs()
        model_watch._ABSENCES[layout.root][kept.model_guid] = model_prefabs.Absence(9, -600.0)
        bpy.ops.paradise_assets.mirror_model_prefabs()
        check(os.path.isfile(kept.path), f"{kept.relative} survives, because a level uses it")


def _models(layout):
    return model_prefabs.list_models(layout)


def _model_path(models, guid: str) -> str | None:
    return next((m.path for m in models if m.guid == guid), None)


def _guid_of(models, path: str) -> str | None:
    return next((m.guid for m in models if m.path == path), None)


def _component_id(layout, type_name: str) -> str | None:
    return next(
        (c.component_id for c in schema.mesh_components(layout.root) if c.type_name == type_name),
        None,
    )


def _marker(prefab) -> str | None:
    """The generated marker, read straight out of the document's root meta."""
    with open(prefab.path, encoding="utf-8") as handle:
        root = prefab_document.loads(handle.read(), prefab.relative).root()
    return root.meta.data.get(model_prefabs.GENERATED_FROM)


def _instantiate(layout, prefab) -> None:
    """Put an instance of ``prefab`` into a level, the thing that must stop a delete."""
    from paradise_assets.document import new_prefab
    from paradise_assets.document.asset_reference import AssetReference

    document = new_prefab.root_only("Holder")
    instance = prefab_document.PrefabObject.with_meta(
        str(uuid.uuid4()), prefab.stem, document.root_guid)
    instance.prefab = AssetReference(prefab.guid, prefab.relative)
    document.objects.append(instance)
    new_prefab.create(layout.resolve("levels/holder.prefab"), layout, document)


def main() -> int:
    source = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else DEFAULT_PROJECT
    if project.locate(source) is None:
        # Not a failure: a clone of this repository alone has no asset project to work on.
        print(f"SKIP: no asset project at or above {source}")
        return 0

    # `default_set` is what puts the entry in `preferences.addons`, which is where the addon's
    # own preferences live; nothing is written to the user's config unless something calls
    # `wm.save_userpref`, and nothing here does (the picker operator, which would, is driven by
    # setting the property directly instead).
    addon_utils.enable("paradise_assets", default_set=True, persistent=False)
    preferences = bpy.context.preferences.addons["paradise_assets"].preferences

    test_registration()

    # After the registration check, which reads the shipped default. A watcher per temp project
    # would outlive the directory it watches.
    preferences.auto_watch = False

    if not check_watcher():
        print("\n(nothing that creates a prefab can be exercised -- stopping here)")
        return 1 if failures else 0

    test_extraction(source)
    test_nested_extraction()
    test_mirror(source)

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
