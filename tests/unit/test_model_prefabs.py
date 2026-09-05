"""The model prefab mirror: models in, actions out.

The diff is pure, clock included, so the grace period before a delete is testable without
waiting for it. The rules being policed are the destructive ones, plus the one that decides
which component a generated prefab authors: a hand-authored prefab is never touched, a
referenced prefab is never deleted, a model that vanished a moment ago is a Finder move until it
has stayed gone, and a rigged model is never authored as a static one.
"""

from __future__ import annotations

import os

from paradise_assets.document import model_prefabs, new_prefab, prefab, schema, well_known
from paradise_assets.document.asset_reference import AssetReference
from paradise_assets.document.project import ProjectLayout

STATIC = schema.MeshComponent(
    "edee8bd8-9321-47db-819d-9bdadf010be4", "ShiningPie.Authoring.StaticMesh", "Mesh"
)
SKINNED = schema.MeshComponent(
    "195846ac-d5e5-49a2-8c98-62ac1914c000", "ShiningPie.Authoring.SkinnedMesh", "Mesh"
)
BOTH = model_prefabs.MeshChoice(STATIC, SKINNED)
FIELDS = schema.MeshFields({(STATIC.type_name, "Mesh"), (SKINNED.type_name, "Mesh")}, "x")

CUBE = model_prefabs.Model(
    AssetReference("11111111-1111-4111-8111-111111111111", "Models/Prim_Cube.glb"), False,
    AssetReference("aaaaaaaa-1111-4111-8111-111111111111", "Models/Prim_Cube.mesh"))
HERO = model_prefabs.Model(
    AssetReference("22222222-2222-4222-8222-222222222222", "Models/Player.glb"), True,
    AssetReference("aaaaaaaa-2222-4222-8222-222222222222", "Models/Player.mesh"))


def project(tmp_path) -> ProjectLayout:
    assets = tmp_path / "assets"
    assets.mkdir(exist_ok=True)
    (assets / "project.toml").write_text('name = "test"\n', encoding="utf-8")
    return ProjectLayout(str(tmp_path))


def moved(model: model_prefabs.Model, path: str) -> model_prefabs.Model:
    """The model AND its mesh document moved together, as a folder move in Finder does."""
    mesh_path = os.path.splitext(path)[0] + ".mesh"
    return model_prefabs.Model(
        AssetReference(model.guid, path), model.skinned,
        AssetReference(model.mesh.guid, mesh_path) if model.mesh else None)


def generated(
    model: model_prefabs.Model, relative: str | None = None, mesh_path: str | None = None
) -> model_prefabs.GeneratedPrefab:
    relative = relative or f"prefabs/models/{model.stem}.prefab"
    return model_prefabs.GeneratedPrefab(
        path=f"/tmp/assets/{relative}",
        relative=relative,
        guid="99999999-9999-4999-8999-999999999999",
        model_guid=model.guid,
        mesh_path=(model.mesh.path if model.mesh else None) if mesh_path is None else mesh_path,
        mesh_component_id=(SKINNED if model.skinned else STATIC).component_id,
        mesh_field="Mesh",
    )


class TestCreate:
    def test_a_model_with_no_prefab_gets_one_under_prefabs_models(self, tmp_path):
        result = model_prefabs.plan(project(tmp_path), BOTH, [CUBE], [])

        assert result.actions == [
            model_prefabs.Create(CUBE, "prefabs/models/Prim_Cube.prefab", STATIC)
        ]

    def test_a_rigged_model_gets_the_skinned_component(self, tmp_path):
        result = model_prefabs.plan(project(tmp_path), BOTH, [HERO], [])

        assert result.actions[0].mesh == SKINNED

    def test_a_model_the_watcher_has_not_given_a_mesh_document_is_skipped(self, tmp_path):
        # Nothing to reference yet: the prefab would have to name the GLB, which the build
        # refuses. The next pass, once the watcher has minted the document, creates it.
        unminted = model_prefabs.Model(CUBE.reference, CUBE.skinned, None)

        result = model_prefabs.plan(project(tmp_path), BOTH, [unminted], [])

        assert len(result.actions) == 1
        assert isinstance(result.actions[0], model_prefabs.Skip)
        assert "mesh document" in result.actions[0].reason

    def test_a_rigged_model_is_skipped_while_no_skinned_component_is_chosen(self, tmp_path):
        # Authoring it as static would produce a prefab that loads, shows the mesh, and is the
        # wrong kind of thing in the game.
        static_only = model_prefabs.MeshChoice(STATIC, None)

        result = model_prefabs.plan(project(tmp_path), static_only, [CUBE, HERO], [])

        assert isinstance(result.actions[0], model_prefabs.Create)
        assert isinstance(result.actions[1], model_prefabs.Skip)
        assert "skinned" in result.actions[1].reason

    def test_a_model_that_already_has_one_is_left_alone(self, tmp_path):
        result = model_prefabs.plan(project(tmp_path), BOTH, [CUBE], [generated(CUBE)])

        assert result.actions == []

    def test_a_hand_authored_prefab_in_the_way_is_skipped_not_overwritten(self, tmp_path):
        layout = project(tmp_path)
        target = layout.resolve("prefabs/models/Prim_Cube.prefab")
        os.makedirs(os.path.dirname(target))
        open(target, "w").close()

        result = model_prefabs.plan(layout, BOTH, [CUBE], [])

        assert isinstance(result.actions[0], model_prefabs.Skip)
        assert "already exists" in result.actions[0].reason

    def test_two_models_with_one_stem_generate_one_prefab_and_a_report(self, tmp_path):
        other = model_prefabs.Model(
            AssetReference("33333333-3333-4333-8333-333333333333", "Props/Prim_Cube.glb"), False,
            AssetReference("aaaaaaaa-3333-4333-8333-333333333333", "Props/Prim_Cube.mesh"))

        result = model_prefabs.plan(project(tmp_path), BOTH, [CUBE, other], [])

        assert isinstance(result.actions[0], model_prefabs.Create)
        assert isinstance(result.actions[1], model_prefabs.Skip)


class TestListModels:
    """``list_models`` reads the model's identity and the mesh document its sidecar records."""

    GLB = b"glTF" + (2).to_bytes(4, "little") + (12).to_bytes(4, "little")

    def _model(self, layout, name: str, sidecar_text: str) -> str:
        directory = layout.resolve("Models")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "wb") as handle:
            handle.write(self.GLB)
        with open(path + ".meta", "w", encoding="utf-8") as handle:
            handle.write(sidecar_text)
        return path

    def test_the_mesh_document_is_read_off_the_glb_sidecar(self, tmp_path):
        layout = project(tmp_path)
        self._model(layout, "Cube.glb", (
            'schema_version = 1\nguid = "11111111-1111-4111-8111-111111111111"\nimporter = "glb"\n'
            '\n[glb]\nmesh = { guid = "AAAAAAAA-1111-4111-8111-111111111111", path = "Models/Cube.mesh" }\n'
        ))

        [model] = model_prefabs.list_models(layout)

        assert model.path == "Models/Cube.glb"
        assert model.mesh == AssetReference("aaaaaaaa-1111-4111-8111-111111111111", "Models/Cube.mesh")

    def test_a_sidecar_the_watcher_has_not_filled_yet_reads_as_no_document(self, tmp_path):
        # The failure this pins: the plain dict tomllib returns for `[glb] mesh` used to be handed
        # to the InlineTable-only codec, which raised on the very sidecar shape the mirror needs.
        layout = project(tmp_path)
        self._model(layout, "NoDomain.glb", (
            'schema_version = 1\nguid = "11111111-1111-4111-8111-111111111111"\n'
        ))
        self._model(layout, "NoMesh.glb", (
            'schema_version = 1\nguid = "22222222-2222-4222-8222-222222222222"\n\n[glb]\nextract = "Models"\n'
        ))
        self._model(layout, "HalfWritten.glb", (
            'schema_version = 1\nguid = "33333333-3333-4333-8333-333333333333"\n'
            '\n[glb]\nmesh = { guid = "nope" }\n'
        ))

        models = {model.stem: model for model in model_prefabs.list_models(layout)}

        assert len(models) == 3
        assert all(model.mesh is None for model in models.values())


class TestMove:
    def test_a_model_moved_by_hand_repoints_the_reference(self, tmp_path):
        elsewhere = moved(CUBE, "Models/Primitives/Prim_Cube.glb")

        result = model_prefabs.plan(project(tmp_path), BOTH, [elsewhere], [generated(CUBE)])

        assert result.actions == [model_prefabs.Move(generated(CUBE), elsewhere)]

    def test_a_prefab_whose_author_removed_the_mesh_reference_is_left_alone(self, tmp_path):
        # Following the model would re-add a component the author deleted; it is theirs now.
        elsewhere = moved(CUBE, "Models/Primitives/Prim_Cube.glb")
        stripped = model_prefabs.GeneratedPrefab(
            path="/tmp/assets/prefabs/models/Prim_Cube.prefab", relative="prefabs/models/Prim_Cube.prefab",
            guid="99999999-9999-4999-8999-999999999999", model_guid=CUBE.guid)

        result = model_prefabs.plan(project(tmp_path), BOTH, [elsewhere], [stripped])

        assert result.actions == []

    def test_a_renamed_model_does_not_rename_its_prefab(self, tmp_path):
        # The reference's guid is the identity and its path a hint, so renaming the file would
        # move it out from under every level already pointing at it, to fix nothing.
        renamed = moved(CUBE, "Models/Boulder.glb")

        result = model_prefabs.plan(project(tmp_path), BOTH, [renamed], [generated(CUBE)])

        assert result.actions == [model_prefabs.Move(generated(CUBE), renamed)]
        assert not hasattr(result.actions[0], "rename_to")


class TestDelete:
    def test_a_missing_model_is_not_deleted_on_the_first_poll(self, tmp_path):
        result = model_prefabs.plan(project(tmp_path), BOTH, [], [generated(CUBE)], now=0.0)

        assert result.actions == []
        assert result.absences[CUBE.guid].polls == 1

    def test_nor_before_the_grace_period_has_passed(self, tmp_path):
        # Two polls a second apart: a Finder move is delete-then-add inside this window.
        first = model_prefabs.plan(project(tmp_path), BOTH, [], [generated(CUBE)], now=0.0)
        second = model_prefabs.plan(
            project(tmp_path), BOTH, [], [generated(CUBE)], absences=first.absences, now=1.0)

        assert second.actions == []
        assert second.absences[CUBE.guid].polls == 2

    def test_a_model_that_stayed_gone_takes_its_prefab_with_it(self, tmp_path):
        first = model_prefabs.plan(project(tmp_path), BOTH, [], [generated(CUBE)], now=0.0)
        second = model_prefabs.plan(
            project(tmp_path), BOTH, [], [generated(CUBE)], absences=first.absences, now=30.0)

        assert second.actions == [model_prefabs.Delete(generated(CUBE), CUBE.guid)]
        assert second.absences == {}

    def test_a_model_that_came_back_forgets_it_was_gone(self, tmp_path):
        first = model_prefabs.plan(project(tmp_path), BOTH, [], [generated(CUBE)], now=0.0)
        second = model_prefabs.plan(
            project(tmp_path), BOTH, [CUBE], [generated(CUBE)], absences=first.absences, now=30.0)

        assert second.actions == []
        assert second.absences == {}

    def test_a_prefab_some_document_instantiates_is_kept_however_long_the_model_is_gone(self, tmp_path):
        entry = generated(CUBE)

        result = model_prefabs.plan(
            project(tmp_path), BOTH, [], [entry], referenced=frozenset({entry.guid}), now=1000.0)

        assert isinstance(result.actions[0], model_prefabs.Skip)
        assert "still" in result.actions[0].reason

    def test_two_prefabs_claiming_one_model_are_reported_rather_than_guessed_between(self, tmp_path):
        result = model_prefabs.plan(
            project(tmp_path), BOTH, [CUBE],
            [generated(CUBE), generated(CUBE, relative="prefabs/models/Copy.prefab")])

        assert any(isinstance(a, model_prefabs.Skip) for a in result.actions)


class TestApply:
    def test_creating_writes_the_marker_and_the_mesh_reference(self, tmp_path, watcher):
        layout = project(tmp_path)
        watcher(layout.root)

        log = model_prefabs.apply(layout, model_prefabs.plan(layout, BOTH, [CUBE], []).actions)

        assert "generated" in log[0]
        path = layout.resolve("prefabs/models/Prim_Cube.prefab")
        with open(path, encoding="utf-8") as handle:
            root = prefab.loads(handle.read(), path).root()
        assert root.name == "Prim_Cube"
        assert root.meta.data[model_prefabs.GENERATED_FROM] == CUBE.guid
        # The MESH DOCUMENT, never the GLB: a GLB ships nothing, and the build refuses a
        # reference to one.
        assert dict(root.component(STATIC.component_id).data["Mesh"]) == {
            "guid": CUBE.mesh.guid, "path": CUBE.mesh.path}
        assert root.component(well_known.TRANSFORM_ID) is not None

    def test_the_marker_is_in_the_document_not_in_the_sidecar(self, tmp_path, watcher):
        # The sidecar belongs to `paradise assets watch`; the addon only reads those. It also
        # keeps `paradise assets verify` quiet about a settings domain no build step reads.
        from paradise_assets.document import sidecar

        layout = project(tmp_path)
        watcher(layout.root)
        model_prefabs.apply(layout, model_prefabs.plan(layout, BOTH, [CUBE], []).actions)

        path = layout.resolve("prefabs/models/Prim_Cube.prefab")
        assert sidecar.read(sidecar.path_for(path)).settings == {}

    def test_the_created_prefab_is_found_again_as_generated(self, tmp_path, watcher):
        layout = project(tmp_path)
        watcher(layout.root)
        model_prefabs.apply(layout, model_prefabs.plan(layout, BOTH, [CUBE], []).actions)

        found = model_prefabs.read_generated(layout, FIELDS)

        assert len(found) == 1
        assert (found[0].model_guid, found[0].mesh_path) == (CUBE.guid, CUBE.mesh.path)
        assert found[0].mesh_component_id == STATIC.component_id

    def test_a_marker_in_another_case_still_identifies_the_model(self, tmp_path, watcher):
        layout = project(tmp_path)
        watcher(layout.root)
        model_prefabs.apply(layout, model_prefabs.plan(layout, BOTH, [CUBE], []).actions)
        path = layout.resolve("prefabs/models/Prim_Cube.prefab")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text.replace(CUBE.guid, CUBE.guid.upper()))

        [found] = model_prefabs.read_generated(layout, FIELDS)

        assert found.model_guid == CUBE.guid

    def test_a_hand_authored_prefab_is_not_reported_as_generated(self, tmp_path, watcher):
        layout = project(tmp_path)
        watcher(layout.root)
        new_prefab.create(layout.resolve("prefabs/box.prefab"), layout, new_prefab.root_only("Box"))

        assert model_prefabs.read_generated(layout, FIELDS) == []

    def test_moving_rewrites_the_reference_and_leaves_the_file_name_alone(self, tmp_path, watcher):
        layout = project(tmp_path)
        watcher(layout.root)
        model_prefabs.apply(layout, model_prefabs.plan(layout, BOTH, [CUBE], []).actions)
        renamed = moved(CUBE, "Models/Boulder.glb")

        found = model_prefabs.read_generated(layout, FIELDS)
        log = model_prefabs.apply(layout, model_prefabs.plan(layout, BOTH, [renamed], found).actions)

        after = model_prefabs.read_generated(layout, FIELDS)[0]
        assert after.mesh_path == renamed.mesh.path
        assert after.relative == "prefabs/models/Prim_Cube.prefab"
        assert "now points at" in log[0]

    def test_a_creation_with_no_watcher_reports_once_rather_than_per_model(self, tmp_path):
        # The batch shares one deadline, so twenty unidentified prefabs are one line and one
        # wait -- not twenty timeouts, which is what a per-file wait made of them.
        layout = project(tmp_path)

        log = model_prefabs.apply(
            layout, model_prefabs.plan(layout, BOTH, [CUBE, HERO], []).actions, timeout=0.2)

        assert len(log) == 1
        assert log[0].startswith("could not identify 2 new prefab(s)")

    def test_the_documents_are_written_even_when_no_identity_arrives(self, tmp_path):
        # They are valid content, and the watcher identifies them the moment one runs. Deleting
        # them because a background process was not started would be the worse failure.
        layout = project(tmp_path)

        model_prefabs.apply(
            layout, model_prefabs.plan(layout, BOTH, [CUBE], []).actions, timeout=0.1)

        assert os.path.isfile(layout.resolve("prefabs/models/Prim_Cube.prefab"))

    def test_deleting_takes_the_sidecar_with_the_document(self, tmp_path, watcher):
        from paradise_assets.document import sidecar

        layout = project(tmp_path)
        watcher(layout.root)
        model_prefabs.apply(layout, model_prefabs.plan(layout, BOTH, [CUBE], []).actions)
        found = model_prefabs.read_generated(layout, FIELDS)

        first = model_prefabs.plan(layout, BOTH, [], found, now=0.0)
        second = model_prefabs.plan(layout, BOTH, [], found, absences=first.absences, now=30.0)
        model_prefabs.apply(layout, second.actions)

        path = layout.resolve("prefabs/models/Prim_Cube.prefab")
        assert not os.path.exists(path)
        assert not os.path.exists(sidecar.path_for(path))


class TestReferencedPrefabs:
    def test_a_prefab_an_open_level_instantiates_is_found(self, tmp_path, watcher):
        layout = project(tmp_path)
        watcher(layout.root)
        model_prefabs.apply(layout, model_prefabs.plan(layout, BOTH, [CUBE], []).actions)
        target = model_prefabs.read_generated(layout, FIELDS)[0]

        level = new_prefab.root_only("Level")
        instance = prefab.PrefabObject.with_meta(
            "44444444-4444-4444-8444-444444444444", "Cube", level.root_guid)
        instance.prefab = AssetReference(target.guid, target.relative)
        level.objects.append(instance)
        new_prefab.create(layout.resolve("levels/main.prefab"), layout, level)

        assert model_prefabs.referenced_prefabs(layout) == frozenset({target.guid})
