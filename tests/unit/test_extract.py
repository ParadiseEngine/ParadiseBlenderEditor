"""Taking a subtree out of a document and leaving an instance behind.

The three properties worth holding on to: the instance keeps the extracted object's identity
(so references to it survive), the prefab's root is a NEW prefab-local identity at the origin,
and the instance carries no copies of the components that moved -- a copy would be an override
that shadows every later edit of the prefab.
"""

from __future__ import annotations

import pytest

from paradise_assets.document import extract, prefab, resolve, well_known
from paradise_assets.document.asset_reference import AssetReference

META = well_known.META_ID
TRANSFORM = well_known.TRANSFORM_ID
BODY = "01b792a0-f12e-4fe9-9867-907ae988b301"
LINK = "22b792a0-f12e-4fe9-9867-907ae988b302"

LEVEL = "aaaaaaaa-0000-4000-8000-000000000001"
RACK = "aaaaaaaa-0000-4000-8000-000000000002"
SHELF = "aaaaaaaa-0000-4000-8000-000000000003"
BOX = "aaaaaaaa-0000-4000-8000-000000000004"
LAMP = "aaaaaaaa-0000-4000-8000-000000000005"

MINTED = "bbbbbbbb-0000-4000-8000-000000000001"

REFERENCE = AssetReference("cccccccc-0000-4000-8000-000000000001", "prefabs/rack.prefab")


def meta(body: str) -> str:
    return f'\n[[objects.components]]\nid = "{META}"\ntype = "meta"\n' + body


def transform(x: float = 0.0) -> str:
    return (
        f'\n[[objects.components]]\nid = "{TRANSFORM}"\ntype = "transform"\n'
        f"Position = [{x}, 0.0, 0.0]\nRotation = [0.0, 0.0, 0.0, 1.0]\nScale = [1.0, 1.0, 1.0]\n"
    )


SOURCE = (
    "schema_version = 1\n"
    "\n[[objects]]\n" + meta(f'Guid = "{LEVEL}"\nName = "Level"\n') + transform()
    + "\n[[objects]]\n" + meta(f'Guid = "{RACK}"\nName = "Rack"\nParent = "{LEVEL}"\n') + transform(4.0)
    + f'\n[[objects.components]]\nid = "{BODY}"\ntype = "Game.Body"\nMass = 3.5\n'
    + "\n[[objects]]\n" + meta(f'Guid = "{SHELF}"\nName = "Shelf"\nParent = "{RACK}"\n') + transform(1.0)
    + "\n[[objects]]\n" + meta(f'Guid = "{BOX}"\nName = "Box"\nParent = "{SHELF}"\n') + transform()
    + "\n[[objects]]\n" + meta(f'Guid = "{LAMP}"\nName = "Lamp"\nParent = "{LEVEL}"\n') + transform()
)


def source() -> prefab.PrefabDocument:
    return prefab.loads(SOURCE, "level.prefab")


def lamp_links_to(guid: str) -> str:
    """The source with a component on Lamp naming *guid* -- the reference an extraction breaks."""
    return SOURCE + (
        f'\n[[objects.components]]\nid = "{LINK}"\ntype = "Game.Link"\nShinesOn = "{guid}"\n'
    )


def run(document=None, guid: str = RACK) -> extract.ExtractResult:
    return extract.extract(document or source(), guid, new_root_guid=MINTED)


def remaining(document=None, guid: str = RACK):
    """What the level looks like afterwards. A method rather than a field, because the prefab's
    asset identity does not exist until the watcher has minted it."""
    return run(document, guid).remaining(REFERENCE)


class TestThePrefab:
    def test_the_subtree_travels_whole(self):
        result = run()

        assert [o.name for o in result.prefab.objects] == ["Rack", "Shelf", "Box"]
        assert result.objects == 3

    def test_the_root_gets_a_new_prefab_local_identity_at_the_origin(self):
        root = run().prefab.root()

        assert root.guid == MINTED
        assert root.parent is None
        assert root.component(TRANSFORM).data["Position"] == [0.0, 0.0, 0.0]

    def test_the_root_keeps_its_name_and_every_other_component(self):
        root = run().prefab.root()

        assert root.name == "Rack"
        assert root.component(BODY).data == {"Mass": 3.5}

    def test_children_keep_their_identities_transforms_and_parents(self):
        by_guid = run().prefab.by_guid()

        assert by_guid[SHELF].parent == MINTED
        assert by_guid[SHELF].component(TRANSFORM).data["Position"] == [1.0, 0.0, 0.0]
        assert by_guid[BOX].parent == SHELF

    def test_the_prefab_is_a_document_the_reader_accepts(self):
        result = run()

        prefab.loads(prefab.dumps(result.prefab), "rack.prefab")

    def test_editing_the_prefab_cannot_reach_back_into_the_source(self):
        result = run()
        result.prefab.root().component(BODY).data["Mass"] = 99.0

        assert source().by_guid()[RACK].component(BODY).data["Mass"] == 3.5


class TestWhatIsLeft:
    def test_the_subtree_becomes_one_instance_in_the_same_slot(self):
        rest = remaining()

        assert [o.name for o in rest.objects] == ["Level", "Rack", "Lamp"]

    def test_the_instance_keeps_the_identity_name_parent_and_placement(self):
        instance = remaining().by_guid()[RACK]

        assert instance.prefab == REFERENCE
        assert (instance.name, instance.parent) == ("Rack", LEVEL)
        assert instance.component(TRANSFORM).data["Position"] == [4.0, 0.0, 0.0]

    def test_the_instance_carries_no_copy_of_what_moved(self):
        # A copy would be an override, and an override shadows the prefab forever.
        instance = remaining().by_guid()[RACK]

        assert [c.id for c in instance.components] == [META, TRANSFORM]

    def test_the_remaining_document_is_one_the_reader_accepts(self):
        prefab.loads(prefab.dumps(remaining()), "level.prefab")

    def test_the_prefab_root_identity_is_document_local_not_an_asset_identity(self):
        # The asset identity comes from the watcher's sidecar and reaches the document only as
        # the instance's `prefab` reference; this one names an object inside the new file.
        result = run()

        assert result.prefab_root_guid == MINTED
        assert result.remaining(REFERENCE).by_guid()[RACK].prefab.guid == REFERENCE.guid

    def test_the_remaining_document_cannot_be_had_without_a_reference(self):
        # Making it a method is what stops a caller writing the level back with a plain object
        # where its instance should be, before the prefab has been identified.
        assert not hasattr(run(), "remaining_document")
        with pytest.raises(TypeError):
            run().remaining()



class TestResolution:
    def test_the_instance_resolves_back_to_the_same_objects_in_the_same_places(self):
        result = run()
        resolved = resolve.resolve(result.remaining(REFERENCE), lambda reference: result.prefab)

        assert resolved.errors == []
        by_name = {o.name: o for o in resolved.document.objects}
        assert set(by_name) == {"Level", "Rack", "Shelf", "Box", "Lamp"}
        # The resolved root IS the instance, so everything that named 'Rack' still does.
        assert by_name["Rack"].guid == RACK
        assert by_name["Rack"].component(TRANSFORM).data["Position"] == [4.0, 0.0, 0.0]
        assert by_name["Shelf"].parent == RACK
        assert by_name["Box"].parent == by_name["Shelf"].guid

    def test_a_child_identity_is_minted_per_instance_and_is_no_longer_the_authored_one(self):
        result = run()
        resolved = resolve.resolve(result.remaining(REFERENCE), lambda reference: result.prefab)

        shelf = next(o for o in resolved.document.objects if o.name == "Shelf")
        assert shelf.guid == resolve.mint_child_guid(RACK, SHELF)
        assert shelf.guid != SHELF


class TestWarnings:
    def test_a_reference_to_an_extracted_child_is_warned_about(self):
        result = run(prefab.loads(lamp_links_to(SHELF), "level.prefab"))

        assert len(result.warnings) == 1
        assert "Shelf" in result.warnings[0] and "Lamp" in result.warnings[0]

    def test_a_reference_to_the_extracted_root_is_not_warned_about(self):
        # The instance keeps that identity, so the reference still resolves.
        result = run(prefab.loads(lamp_links_to(RACK), "level.prefab"))

        assert result.warnings == []

    def test_a_guid_nested_in_an_array_is_still_found(self):
        text = SOURCE + (
            f'\n[[objects.components]]\nid = "{LINK}"\ntype = "Game.Link"\n'
            f'Targets = [{{ guid = "{BOX}", path = "x" }}]\n'
        )
        result = run(prefab.loads(text, "level.prefab"))

        assert len(result.warnings) == 1
        assert "Box" in result.warnings[0]


class TestCarriers:
    def test_a_carrier_belonging_to_an_instance_inside_the_subtree_travels_with_it(self):
        materials = "bdc4fc87-d7b4-41f1-bc90-fc827005adfc"
        child_local = "dddddddd-0000-4000-8000-000000000009"
        text = SOURCE + (
            "\n[[objects]]\n"
            + meta(f'Parent = "{SHELF}"\nTarget = "{child_local}"\n')
            + f'\n[[objects.components]]\nid = "{materials}"\nSlots = [{{}}, {{}}]\n'
        )
        result = run(prefab.loads(text, "level.prefab"))

        assert result.carriers == 1
        assert [o.target for o in result.prefab.objects] == [None, None, None, child_local]
        assert all(o.target is None for o in result.remaining(REFERENCE).objects)

    def test_a_carrier_on_the_extracted_object_itself_is_repointed_at_the_new_root(self):
        child_local = "dddddddd-0000-4000-8000-000000000009"
        text = SOURCE + "\n[[objects]]\n" + meta(f'Parent = "{RACK}"\nTarget = "{child_local}"\n')
        result = run(prefab.loads(text, "level.prefab"))

        carrier = next(o for o in result.prefab.objects if o.target is not None)
        assert carrier.parent == MINTED


class TestRefusals:
    def test_the_document_root_cannot_be_extracted(self):
        with pytest.raises(extract.ExtractError, match="root"):
            run(guid=LEVEL)

    def test_an_unknown_identity_is_refused(self):
        with pytest.raises(extract.ExtractError, match="no object"):
            run(guid="eeeeeeee-0000-4000-8000-000000000001")

    def test_text_that_is_not_an_identity_is_refused(self):
        with pytest.raises(extract.ExtractError, match="not an object identity"):
            run(guid="Rack")

    def test_an_override_carrier_is_refused(self):
        carrier_guid = "dddddddd-0000-4000-8000-000000000001"
        text = SOURCE + "\n[[objects]]\n" + meta(
            f'Guid = "{carrier_guid}"\nParent = "{RACK}"\nTarget = "{SHELF}"\n'
        )
        with pytest.raises(extract.ExtractError, match="override"):
            run(prefab.loads(text, "level.prefab"), guid=carrier_guid)
