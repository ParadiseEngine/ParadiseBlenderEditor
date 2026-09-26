"""An object that owns its geometry: which GLB is whose, the engine's slot order, and where a new
one may be written. The Blender half (building, exporting) is ``tests/integration``'s.
"""

from __future__ import annotations

import math
import os

import pytest
from test_gltf import glb

from paradise_assets.document import editable_mesh as ownership
from paradise_assets.document.asset_reference import AssetReference
from paradise_assets.document.project import ProjectLayout

GUID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
OTHER = "ffffffff-eeee-4ddd-8ccc-bbbbbbbbbbbb"
MESH_GUID = "12345678-1234-4234-8234-123456789abc"


def nodes_document(nodes, roots, scene=None) -> dict:
    document = {
        "nodes": nodes,
        "scenes": [{"nodes": roots}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
    }
    if scene is not None:
        document["scene"] = scene
    return document


def translation(world) -> tuple[float, ...]:
    return tuple(round(value, 6) for value in world[12:15])


class TestEngineOrder:
    """Slot ``i`` is primitive ``i`` in the order the engine bakes instances; a different order
    here would put every material on the wrong part without an error anywhere."""

    def test_depth_first_from_the_roots_children_in_listed_order(self):
        document = nodes_document(
            [
                {"mesh": 0},                                              # 0
                {"mesh": 0},                                              # 1
                {"mesh": 0, "children": [3, 1], "translation": [1, 0, 0]},  # 2
                {"mesh": 0, "translation": [0, 2, 0]},                     # 3
                {"children": []},                                         # 4: no mesh
            ],
            roots=[2, 0, 4],
        )

        instances = ownership.mesh_instances(document)

        assert [node for node, _mesh, _world in instances] == [2, 3, 1, 0]
        assert translation(instances[1][2]) == (1.0, 2.0, 0.0), "a child is placed by its parent"

    def test_a_parents_rotation_and_scale_carry_the_childs_offset(self):
        half = math.sqrt(0.5)
        document = nodes_document(
            [
                {"rotation": [0, 0, half, half], "scale": [2, 2, 2], "children": [1]},
                {"mesh": 0, "translation": [1, 0, 0]},
            ],
            roots=[0],
        )

        (_node, _mesh, world), = ownership.mesh_instances(document)

        # A quarter turn about +Z takes +X to +Y; the parent's scale doubles the offset.
        assert translation(world) == (0.0, 2.0, 0.0)

    def test_an_explicit_matrix_wins_over_trs(self):
        matrix = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 5, 6, 7, 1]
        document = nodes_document([{"mesh": 0, "matrix": matrix, "translation": [9, 9, 9]}], roots=[0])

        assert translation(ownership.mesh_instances(document)[0][2]) == (5.0, 6.0, 7.0)

    def test_the_default_scene_is_the_one_walked(self):
        document = nodes_document([{"mesh": 0}, {"mesh": 0}], roots=[0], scene=1)
        document["scenes"].append({"nodes": [1]})

        assert [node for node, _mesh, _world in ownership.mesh_instances(document)] == [1]

    def test_a_cycle_ends_the_walk_instead_of_hanging_it(self):
        document = nodes_document([{"mesh": 0, "children": [0]}], roots=[0])

        assert len(ownership.mesh_instances(document)) == 1


class TestUnsupported:
    @pytest.mark.parametrize(("change", "words"), [
        ({"skins": [{"joints": [0]}]}, "rigged"),
        ({"animations": [{"channels": []}]}, "animated"),
        ({"extensionsRequired": ["KHR_draco_mesh_compression"]}, "compressed"),
    ])
    def test_models_whose_geometry_cannot_be_rebuilt_are_named(self, change, words):
        document = nodes_document([{"mesh": 0}], roots=[0]) | change

        assert words in ownership.unsupported(document)

    def test_lines_are_not_a_mesh(self):
        document = nodes_document([{"mesh": 0}], roots=[0])
        document["meshes"][0]["primitives"][0]["mode"] = 1

        assert "triangles" in ownership.unsupported(document)

    def test_a_primitive_without_positions_is_refused(self):
        document = nodes_document([{"mesh": 0}], roots=[0])
        document["meshes"][0]["primitives"][0]["attributes"] = {"NORMAL": 0}

        assert "POSITION" in ownership.unsupported(document)

    def test_a_static_triangle_mesh_is_supported(self):
        assert ownership.unsupported(nodes_document([{"mesh": 0}], roots=[0])) is None


class TestOwnership:
    def owned(self, tmp_path, owner, name="wall.glb"):
        path = tmp_path / name
        path.write_bytes(glb({"scenes": [{"nodes": [], "extras": {ownership.OWNER_EXTRA: owner}}]}))
        return str(path)

    def test_the_owner_is_read_from_the_default_scene(self, tmp_path):
        path = self.owned(tmp_path, GUID.upper())

        assert ownership.owner_of(path) == GUID
        assert ownership.owns(path, GUID.upper()) and not ownership.owns(path, OTHER)

    def test_a_shared_model_has_no_owner(self, tmp_path):
        path = tmp_path / "crate.glb"
        path.write_bytes(glb({"scenes": [{"nodes": []}]}))

        assert ownership.owner_of(str(path)) is None
        assert not ownership.owns(str(path), GUID)

    def test_a_rewritten_file_is_read_again(self, tmp_path):
        path = self.owned(tmp_path, GUID)
        assert ownership.owns(path, GUID)

        self.owned(tmp_path, OTHER)
        os.utime(path, ns=(1, 1))   # same size, so the stamp must differ by time

        assert ownership.owns(path, OTHER) and not ownership.owns(path, GUID)


def project(tmp_path, extraction='[extract]\nmeshes = "meshes"\n') -> ProjectLayout:
    (tmp_path / "assets/levels").mkdir(parents=True)
    (tmp_path / "assets/project.toml").write_text("schema_version = 1\n" + extraction)
    (tmp_path / "assets/levels/arena.prefab").write_text("")
    return ProjectLayout(str(tmp_path))


def mesh_document(layout, relative, glb_relative):
    path = layout.resolve(relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f'schema_version = 1\nsource = {{ guid = "{OTHER}", path = "{glb_relative}" }}\n')
    return path


class TestPlanTarget:
    def test_the_glb_goes_beside_its_document_named_for_the_object(self, tmp_path):
        layout = project(tmp_path)

        target = ownership.plan_target(layout, layout.resolve("levels/arena.prefab"), "Wall #3", GUID)

        assert target == layout.resolve(f"levels/arena/Wall_3_{GUID[:8]}.glb")

    def test_a_file_it_does_not_own_is_refused(self, tmp_path):
        layout = project(tmp_path)
        target = layout.resolve(f"levels/arena/Wall_{GUID[:8]}.glb")
        os.makedirs(os.path.dirname(target))
        with open(target, "wb") as handle:
            handle.write(glb({"scenes": [{"nodes": []}]}))

        with pytest.raises(ownership.EditableMeshError, match="already exists"):
            ownership.plan_target(layout, layout.resolve("levels/arena.prefab"), "Wall", GUID)

    def test_its_own_file_is_reused_for_a_retry(self, tmp_path):
        layout = project(tmp_path)
        target = layout.resolve(f"levels/arena/Wall_{GUID[:8]}.glb")
        os.makedirs(os.path.dirname(target))
        with open(target, "wb") as handle:
            handle.write(glb({"scenes": [{"nodes": [], "extras": {ownership.OWNER_EXTRA: GUID}}]}))
        mesh_document(layout, f"meshes/Wall_{GUID[:8]}.mesh", f"levels/arena/Wall_{GUID[:8]}.glb")

        assert ownership.plan_target(layout, layout.resolve("levels/arena.prefab"), "Wall", GUID) == target

    def test_a_stray_sidecar_is_not_taken_over(self, tmp_path):
        layout = project(tmp_path)
        target = layout.resolve(f"levels/arena/Wall_{GUID[:8]}.glb")
        os.makedirs(os.path.dirname(target))
        with open(target + ".meta", "w", encoding="utf-8") as handle:
            handle.write(f'schema_version = 1\nguid = "{OTHER}"\n')

        with pytest.raises(ownership.EditableMeshError, match="without its GLB"):
            ownership.plan_target(layout, layout.resolve("levels/arena.prefab"), "Wall", GUID)

    def test_another_glbs_mesh_in_the_namespace_is_refused(self, tmp_path):
        layout = project(tmp_path)
        mesh_document(layout, f"meshes/Wall_{GUID[:8]}.mesh", "models/Wall.glb")

        with pytest.raises(ownership.EditableMeshError, match="namespace"):
            ownership.plan_target(layout, layout.resolve("levels/arena.prefab"), "Wall", GUID)

    def test_a_folder_linked_outside_assets_is_refused(self, tmp_path):
        layout = project(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / "assets/levels/arena").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ownership.EditableMeshError, match="outside"):
            ownership.plan_target(layout, layout.resolve("levels/arena.prefab"), "Wall", GUID)


class TestMeshReference:
    """The ``.mesh`` document is the watcher's to mint; the reference exists once it and its
    identity do, found through the GLB's own extraction record."""

    def minted(self, layout, *, mesh=True, identity=True, names="levels/arena/Wall.glb"):
        glb_path = layout.resolve("levels/arena/Wall.glb")
        os.makedirs(os.path.dirname(glb_path), exist_ok=True)
        with open(glb_path, "wb") as handle:
            handle.write(glb({"scenes": [{"nodes": []}]}))
        with open(glb_path + ".meta", "w", encoding="utf-8") as handle:
            handle.write(
                f'schema_version = 1\nguid = "{OTHER}"\n\n[extract]\n'
                f'parts = [{{ kind = "meshes", path = "meshes/Wall.mesh", guid = "{MESH_GUID}" }}]\n'
            )
        if mesh:
            path = mesh_document(layout, "meshes/Wall.mesh", names)
            if identity:
                with open(path + ".meta", "w", encoding="utf-8") as handle:
                    handle.write(f'schema_version = 1\nguid = "{MESH_GUID.upper()}"\n')
        return glb_path

    def test_the_minted_document_is_referenced_by_its_own_identity(self, tmp_path):
        layout = project(tmp_path)
        glb_path = self.minted(layout)

        assert ownership.mesh_reference(glb_path, layout) == AssetReference(MESH_GUID, "meshes/Wall.mesh")

    def test_nothing_before_the_document_has_an_identity(self, tmp_path):
        layout = project(tmp_path)

        assert ownership.mesh_reference(self.minted(layout, identity=False), layout) is None
        assert ownership.mesh_reference(self.minted(layout, mesh=False), layout) is None

    def test_a_document_that_names_another_glb_is_not_this_ones(self, tmp_path):
        layout = project(tmp_path)

        assert ownership.mesh_reference(self.minted(layout, names="models/Wall.glb"), layout) is None


def test_material_slots_come_from_the_first_payload_that_binds_materials():
    payloads = [
        {"Mesh": {"guid": OTHER, "path": "meshes/Wall.mesh"}},
        {"Slots": [{"guid": OTHER, "path": "materials/a.material"}, {}, "materials/b.material"]},
        {"Slots": [{"guid": OTHER, "path": "materials/ignored.material"}]},
    ]

    assert ownership.material_slots(payloads) == ["materials/a.material", None, "materials/b.material"]
    assert ownership.material_slots([{"Mesh": "meshes/Wall.mesh"}, None]) is None
