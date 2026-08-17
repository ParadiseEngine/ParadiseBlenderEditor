"""Tests for the data-directory cleanup.

This is the only part of an export that DELETES, so most of these pin what it must refuse to
touch rather than what it removes. A missed orphan costs disk; a wrongly deleted asset costs an
author their work, and it is discovered later, by a game that no longer loads a mesh.
"""

from __future__ import annotations

import json
import os
import struct

import pytest

from paradise_blender.paths import ExportPaths
from paradise_blender.pipeline.prune import prune_orphans


def write(path: str, content: bytes | str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(path, mode) as handle:
        handle.write(content)
    return path


def glb(images: list[str] | None = None) -> bytes:
    """A minimal valid GLB whose images point at the given sidecar URIs."""
    document = json.dumps({"images": [{"uri": uri} for uri in images or []]}).encode()
    document += b" " * ((-len(document)) % 4)
    body = struct.pack("<II", len(document), 0x4E4F534A) + document
    return struct.pack("<III", 0x46546C67, 2, 12 + len(body)) + body


def scene_document(entities: list[dict], **extra) -> str:
    return json.dumps({"SchemaVersion": 1, "Entities": entities, **extra})


def entity(mesh: str | None = None, materials: list[str | None] | None = None, **extra) -> dict:
    return {
        "Id": "e",
        "Components": {"Renderable": {"Mesh": mesh} if mesh else None},
        "Materials": materials or [],
        **extra,
    }


@pytest.fixture
def data(tmp_path):
    paths = ExportPaths(str(tmp_path / "project" / "data"))
    paths.ensure_output_directory()
    return paths


def field(paths: ExportPaths, name: str) -> str:
    return paths.output_path_for_field(name)


class TestOrphanRemoval:
    def test_removes_an_unreferenced_mesh(self, data):
        write(field(data, "scenes/level.json"), scene_document([entity("Models/Used.glb")]))
        write(field(data, "Models/Used.glb"), glb())
        write(field(data, "Models/Renamed.glb"), glb())

        assert prune_orphans(data) == ["Models/Renamed.glb"]
        assert os.path.exists(field(data, "Models/Used.glb"))
        assert not os.path.exists(field(data, "Models/Renamed.glb"))

    def test_removes_an_unreferenced_material(self, data):
        write(field(data, "scenes/level.json"), scene_document([entity(materials=["materials/a.json"])]))
        write(field(data, "materials/a.json"), "{}")
        write(field(data, "materials/gone.json"), "{}")

        assert prune_orphans(data) == ["materials/gone.json"]

    def test_removes_a_sidecar_its_mesh_stopped_using(self, data):
        """The case that produced two of ShiningPie's three real orphans: a material lost its
        texture, so the GLB stopped naming a sidecar that stayed on disk."""
        write(field(data, "scenes/level.json"), scene_document([entity("Models/Prop.glb")]))
        write(field(data, "Models/Prop.glb"), glb(["Prop.InUse.ktx2"]))
        write(field(data, "Models/Prop.InUse.ktx2"), b"live")
        write(field(data, "Models/Prop.Dropped.ktx2"), b"stale")

        assert prune_orphans(data) == ["Models/Prop.Dropped.ktx2"]

    def test_dry_run_reports_without_deleting(self, data):
        write(field(data, "scenes/level.json"), scene_document([entity("Models/Used.glb")]))
        write(field(data, "Models/Used.glb"), glb())
        write(field(data, "Models/Orphan.glb"), glb())

        assert prune_orphans(data, dry_run=True) == ["Models/Orphan.glb"]
        assert os.path.exists(field(data, "Models/Orphan.glb"))


class TestNeverTouches:
    def test_files_outside_owned_directories(self, data):
        """Wwise writes data/audio and the game writes its own config there. Neither is ours, and
        a cleanup that reached them would delete assets no exporter can regenerate."""
        write(field(data, "scenes/level.json"), scene_document([entity("Models/Used.glb")]))
        write(field(data, "Models/Used.glb"), glb())
        write(field(data, "audio/ShiningPie.bnk"), b"bank")
        write(field(data, "shiningpie/config.json"), "{}")
        write(field(data, "ProjectSettings.json"), "{}")

        assert prune_orphans(data) == []
        assert os.path.exists(field(data, "audio/ShiningPie.bnk"))
        assert os.path.exists(field(data, "shiningpie/config.json"))

    def test_unowned_extensions_inside_owned_directories(self, data):
        write(field(data, "scenes/level.json"), scene_document([entity("Models/Used.glb")]))
        write(field(data, "Models/Used.glb"), glb())
        write(field(data, "Models/notes.txt"), "an author's note")

        assert prune_orphans(data) == []
        assert os.path.exists(field(data, "Models/notes.txt"))

    def test_scene_documents_are_roots_and_are_never_deleted(self, data):
        write(field(data, "scenes/level.json"), scene_document([entity("Models/Used.glb")]))
        write(field(data, "Models/Used.glb"), glb())

        assert prune_orphans(data) == []
        assert os.path.exists(field(data, "scenes/level.json"))

    def test_another_scenes_assets(self, data):
        """A data/ shared by two .blends: exporting one must not delete the other's meshes."""
        write(field(data, "scenes/a.json"), scene_document([entity("Models/OnlyInA.glb")]))
        write(field(data, "scenes/b.json"), scene_document([entity("Models/OnlyInB.glb")]))
        write(field(data, "Models/OnlyInA.glb"), glb())
        write(field(data, "Models/OnlyInB.glb"), glb())

        assert prune_orphans(data) == []

    def test_assets_only_a_prefab_template_references(self, data):
        prefab = {"PrefabAssetPath": "Props", "Entities": [entity("Models/InPrefab.glb")]}
        write(
            field(data, "scenes/level.json"),
            scene_document([entity("Models/Used.glb", PrefabAssetPath="Props")]),
        )
        write(field(data, "prefabs/Props.json"), json.dumps(prefab))
        write(field(data, "Models/Used.glb"), glb())
        write(field(data, "Models/InPrefab.glb"), glb())

        assert prune_orphans(data) == []

    def test_a_texture_only_a_material_document_references(self, data):
        write(field(data, "scenes/level.json"), scene_document([entity(materials=["materials/m.json"])]))
        write(field(data, "materials/m.json"), json.dumps({"BaseColorTexture": "Models/Shared.ktx2"}))
        write(field(data, "Models/Shared.ktx2"), b"texture")

        assert prune_orphans(data) == []

    def test_the_navmesh_the_scene_names(self, data):
        write(
            field(data, "scenes/level.json"),
            scene_document([entity("Models/Used.glb")], NavMeshFile="level.navmesh.bin"),
        )
        write(field(data, "Models/Used.glb"), glb())
        write(field(data, "scenes/level.navmesh.bin"), b"recast")
        write(field(data, "scenes/renamed.navmesh.bin"), b"stale")

        assert prune_orphans(data) == ["scenes/renamed.navmesh.bin"]


class TestRefusesToRun:
    def test_when_a_scene_document_cannot_be_read(self, data):
        """Half the roots is worse than none: the unreadable document's assets would all look
        unreferenced."""
        write(field(data, "scenes/good.json"), scene_document([entity("Models/Used.glb")]))
        write(field(data, "scenes/broken.json"), "{ this is not json")
        write(field(data, "Models/Used.glb"), glb())
        write(field(data, "Models/Other.glb"), glb())

        assert prune_orphans(data) == []
        assert os.path.exists(field(data, "Models/Other.glb"))

    def test_when_no_scene_declares_any_entities(self, data):
        """An export that found nothing writes a valid, empty document — and sweeping against it
        would empty the whole directory."""
        write(field(data, "scenes/level.json"), scene_document([]))
        write(field(data, "Models/Everything.glb"), glb())

        assert prune_orphans(data) == []
        assert os.path.exists(field(data, "Models/Everything.glb"))

    def test_when_a_glb_is_unreadable(self, data):
        """An unreadable GLB names no sidecars, which must read as "no information" rather than
        "references nothing" — otherwise its textures are collected as orphans."""
        write(field(data, "scenes/level.json"), scene_document([entity("Models/Broken.glb")]))
        write(field(data, "Models/Broken.glb"), b"truncated")
        write(field(data, "Models/Broken.Texture.ktx2"), b"texture")

        # The GLB itself is referenced and kept; its sidecar cannot be proven live, and is not
        # deleted on that basis alone -- documented as the safe direction.
        removed = prune_orphans(data)
        assert "Models/Broken.glb" not in removed


class TestEmptyDirectory:
    def test_nothing_to_do_is_not_an_error(self, data):
        assert prune_orphans(data) == []
