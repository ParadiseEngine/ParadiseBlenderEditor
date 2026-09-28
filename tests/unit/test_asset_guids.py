"""Every asset collection of a .blend carries a GUID of its own, minted on save and kept by the
collection the project already knows under it."""

from __future__ import annotations

import itertools

from paradise_assets.document import asset_guids

LAMP = "0b5e8a52-4f43-4c1e-9a55-3d2b8f1c6e70"
POST = "1c6e8a52-4f43-4c1e-9a55-3d2b8f1c6e71"


def minter():
    counter = itertools.count(1)
    return lambda: f"00000000-0000-4000-8000-{next(counter):012d}"


def nothing_recorded():
    raise AssertionError("the project record was read without a GUID two collections share")


def test_a_collection_without_a_usable_guid_gets_a_fresh_one():
    changes = asset_guids.assignments(
        [("Lamp", None), ("Post", "Post"), ("Zero", "00000000-0000-0000-0000-000000000000"), ("Kept", LAMP)],
        nothing_recorded, minter())

    assert changes == {
        "Lamp": "00000000-0000-4000-8000-000000000001",
        "Post": "00000000-0000-4000-8000-000000000002",
        "Zero": "00000000-0000-4000-8000-000000000003",
    }


def test_a_guid_in_another_spelling_is_rewritten_canonical():
    changes = asset_guids.assignments([("Lamp", LAMP.upper()), ("Post", POST.replace("-", ""))],
                                      nothing_recorded, minter())

    assert changes == {"Lamp": LAMP, "Post": POST}


def test_a_copy_gets_a_fresh_guid_and_the_recorded_collection_keeps_its_own():
    # Blender copies custom properties on duplicate; "Lamp.001" sorts after "Lamp", but here
    # the project knows the GUID by the copy's name, so the other one is the newcomer.
    changes = asset_guids.assignments([("Lamp", LAMP), ("Lamp_Old", LAMP)],
                                      lambda: {LAMP: {"Lamp_Old"}}, minter())

    assert changes == {"Lamp": "00000000-0000-4000-8000-000000000001"}


def test_without_a_record_the_first_by_name_keeps_the_guid():
    changes = asset_guids.assignments([("Lamp.001", LAMP), ("Lamp", LAMP.upper()), ("Post", POST)],
                                      lambda: {}, minter())

    assert changes == {"Lamp.001": "00000000-0000-4000-8000-000000000001", "Lamp": LAMP}


def test_a_record_naming_none_of_the_sharers_falls_back_to_the_first_by_name():
    changes = asset_guids.assignments([("B", LAMP), ("A", LAMP), ("C", LAMP)],
                                      lambda: {LAMP: {"Renamed"}}, minter())

    assert changes == {"B": "00000000-0000-4000-8000-000000000001",
                       "C": "00000000-0000-4000-8000-000000000002"}


def test_the_project_record_is_the_extracted_parts_and_their_documents_name_hints(tmp_path):
    (tmp_path / "assets" / "models").mkdir(parents=True)
    (tmp_path / "assets" / "project.toml").write_text('name = "test"\n', encoding="utf-8")
    blend = tmp_path / "assets" / "models" / "lamps.blend"
    blend.write_bytes(b"BLENDER")
    (tmp_path / "assets" / "models" / "Lamp.mesh").write_text(
        'source = { guid = "cfd24c3a-5972-53fd-a757-0b2c3b610597", path = "models/lamps.blend" }\n'
        f'asset = {{ guid = "{LAMP}", name = "Lamp_Renamed" }}\n', encoding="utf-8")
    (tmp_path / "assets" / "models" / "lamps.blend.meta").write_text(
        'schema_version = 1\nguid = "cfd24c3a-5972-53fd-a757-0b2c3b610597"\n\n[extract]\nparts = [\n'
        f'  {{ asset = "{LAMP.upper()}", guid = "{POST}", index = 0, kind = "mesh", name = "Lamp", '
        'ownership = "owned", path = "models/Lamp.mesh" },\n'
        f'  {{ asset = "{POST}", guid = "{LAMP}", index = 0, kind = "material", name = "Brass", '
        'ownership = "owned", path = "models/Brass.material" },\n'
        '  { guid = "cfd24c3a-5972-53fd-a757-0b2c3b610598", index = 0, kind = "mesh", name = "Whole", '
        'ownership = "owned", path = "models/Whole.mesh" },\n'
        ']\n', encoding="utf-8")

    recorded = asset_guids.recorded_names(str(blend))

    assert recorded == {LAMP: {"Lamp", "Lamp_Renamed"}, POST: {"Brass"}}

def test_a_blend_outside_a_project_or_never_extracted_records_nothing(tmp_path):
    loose = tmp_path / "loose.blend"
    loose.write_bytes(b"BLENDER")
    assert asset_guids.recorded_names(str(loose)) == {}

    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "project.toml").write_text('name = "test"\n', encoding="utf-8")
    fresh = tmp_path / "assets" / "fresh.blend"
    fresh.write_bytes(b"BLENDER")
    assert asset_guids.recorded_names(str(fresh)) == {}
