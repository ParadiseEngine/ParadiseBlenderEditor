"""A mesh document points the viewport at the GLB it was compiled from."""

from __future__ import annotations

from paradise_assets.document import mesh_document
from paradise_assets.document.model_source import Model
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

    expected = Model(layout.resolve("Models/Player.glb"))
    assert mesh_document.source_for(layout, "Models/Player.skinnedmesh") == expected
    assert mesh_document.displayable(layout, "Models/Player.skinnedmesh") == expected


def test_a_glb_is_displayed_as_itself(tmp_path):
    layout = project(tmp_path)

    assert mesh_document.displayable(layout, "Models/Crate.glb") == Model(layout.resolve("Models/Crate.glb"))
    assert mesh_document.is_document("Models/Crate.mesh")
    assert not mesh_document.is_document("Models/Crate.glb")


def test_a_missing_or_unreadable_document_displays_nothing(tmp_path):
    layout = project(tmp_path)
    (tmp_path / "assets" / "Models" / "Broken.mesh").write_text("source = [\n", encoding="utf-8")
    (tmp_path / "assets" / "Models" / "Sourceless.mesh").write_text("schema_version = 1\n", encoding="utf-8")

    assert mesh_document.source_for(layout, "Models/Absent.mesh") is None
    assert mesh_document.source_for(layout, "Models/Broken.mesh") is None
    assert mesh_document.source_for(layout, "Models/Sourceless.mesh") is None


LAMP = "0b5e8a52-4f43-4c1e-9a55-3d2b8f1c6e70"


def test_a_document_of_one_asset_names_the_blend_and_the_asset_by_guid(tmp_path):
    layout = project(tmp_path)
    (tmp_path / "assets" / "Models" / "Lamp_A.mesh").write_text(
        'schema_version = 1\n'
        'source = { guid = "cfd24c3a-5972-53fd-a757-0b2c3b610597", path = "Models/Lamps.blend" }\n'
        f'asset = {{ guid = "{LAMP.upper()}", name = "Lamp_A" }}\nslot = "mesh"\n',
        encoding="utf-8")

    found = mesh_document.displayable(layout, "Models/Lamp_A.mesh")

    assert found == Model(layout.resolve("Models/Lamps.blend"), LAMP), "the guid is read canonical"
    assert found.name == "Lamp_A"
    assert found.label == "Lamp_A in Lamps.blend"


def test_the_name_is_a_hint_and_not_the_identity():
    blend = "/p/assets/Models/Lamps.blend"

    assert Model(blend, LAMP, "Lamp_A") == Model(blend, LAMP, "Lamp_Renamed")
    assert Model(blend, LAMP, "Lamp_A") != Model(blend, "1b5e8a52-4f43-4c1e-9a55-3d2b8f1c6e70", "Lamp_A")


def test_an_asset_that_is_not_a_guid_and_a_name_displays_nothing(tmp_path):
    layout = project(tmp_path)
    for name, asset in (
        ("Bare", '"Lamp_A"'),
        ("Nameless", f'{{ guid = "{LAMP}" }}'),
        ("Guidless", '{ name = "Lamp_A" }'),
        ("Empty", '{ guid = "00000000-0000-0000-0000-000000000000", name = "Lamp_A" }'),
        ("NotGuid", '{ guid = "Lamp_A", name = "Lamp_A" }'),
        ("NumberName", f'{{ guid = "{LAMP}", name = 3 }}'),
    ):
        (tmp_path / "assets" / "Models" / f"{name}.mesh").write_text(
            f'source = {{ guid = "cfd24c3a-5972-53fd-a757-0b2c3b610597", path = "Models/Lamps.blend" }}\n'
            f'asset = {asset}\n', encoding="utf-8")

        assert mesh_document.source_for(layout, f"Models/{name}.mesh") is None, name
