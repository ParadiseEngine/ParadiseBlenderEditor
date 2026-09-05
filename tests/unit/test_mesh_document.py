"""A mesh document points the viewport at the GLB it was compiled from."""

from __future__ import annotations

from paradise_assets.document import mesh_document
from paradise_assets.document.project import ProjectLayout

DOCUMENT = (
    'schema_version = 1\n'
    'source = { guid = "cfd24c3a-5972-53fd-a757-0b2c3b610597", path = "Models/Player.glb" }\n'
    'slot = "skinnedmesh"\n'
    'skeleton = { guid = "c8ab77d9-c5cb-4d31-bb3e-d7cbd5ae8ff3", path = "Models/Player.skeleton" }\n'
)


def project(tmp_path) -> ProjectLayout:
    (tmp_path / "assets" / "Models").mkdir(parents=True)
    (tmp_path / "assets" / "project.toml").write_text('name = "test"\n', encoding="utf-8")
    return ProjectLayout(str(tmp_path))


def test_a_document_resolves_to_the_glb_it_names(tmp_path):
    layout = project(tmp_path)
    (tmp_path / "assets" / "Models" / "Player.skinnedmesh").write_text(DOCUMENT, encoding="utf-8")

    expected = layout.resolve("Models/Player.glb")
    assert mesh_document.glb_for(layout, "Models/Player.skinnedmesh") == expected
    assert mesh_document.displayable(layout, "Models/Player.skinnedmesh") == expected


def test_a_glb_is_displayed_as_itself(tmp_path):
    layout = project(tmp_path)

    assert mesh_document.displayable(layout, "Models/Crate.glb") == layout.resolve("Models/Crate.glb")
    assert mesh_document.is_document("Models/Crate.mesh")
    assert not mesh_document.is_document("Models/Crate.glb")


def test_a_missing_or_unreadable_document_displays_nothing(tmp_path):
    layout = project(tmp_path)
    (tmp_path / "assets" / "Models" / "Broken.mesh").write_text("source = [\n", encoding="utf-8")
    (tmp_path / "assets" / "Models" / "Sourceless.mesh").write_text("schema_version = 1\n", encoding="utf-8")

    assert mesh_document.glb_for(layout, "Models/Absent.mesh") is None
    assert mesh_document.glb_for(layout, "Models/Broken.mesh") is None
    assert mesh_document.glb_for(layout, "Models/Sourceless.mesh") is None
