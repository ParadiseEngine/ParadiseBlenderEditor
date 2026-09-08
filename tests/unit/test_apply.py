"""Pushing an instance's overrides back into the prefab they shadow.

The asymmetry is the thing to defend: the instance ROOT's placement and name are the LEVEL's, so
they never apply; a CHILD carrier's transform and name are corrections to the prefab, so they do.
Getting that backwards moves every instance of the prefab to one instance's spot.
"""

from __future__ import annotations

import pytest

from paradise_assets.document import apply, prefab, well_known
from paradise_assets.document.asset_reference import AssetReference

META = well_known.META_ID
TRANSFORM = well_known.TRANSFORM_ID
TAG = "01b792a0-f12e-4fe9-9867-907ae988b301"
BODY = "22b792a0-f12e-4fe9-9867-907ae988b302"

ROOT_LOCAL = "aaaaaaaa-0000-4000-8000-000000000001"
CHILD_LOCAL = "aaaaaaaa-0000-4000-8000-000000000002"
GRANDCHILD_LOCAL = "aaaaaaaa-0000-4000-8000-000000000003"

LEVEL = "bbbbbbbb-0000-4000-8000-000000000001"
INSTANCE = "410f381b-fc6e-5a66-a70a-698972a199b5"
SECOND = "510f381b-fc6e-5a66-a70a-698972a199b5"
FLOOR = "bbbbbbbb-0000-4000-8000-000000000002"

LAMP = AssetReference("cccccccc-0000-4000-8000-000000000001", "prefabs/lamp.prefab")


def meta(body: str) -> str:
    return f'\n[[objects.components]]\nid = "{META}"\ntype = "meta"\n' + body


def transform(x: float = 0.0) -> str:
    return (
        f'\n[[objects.components]]\nid = "{TRANSFORM}"\ntype = "transform"\n'
        f"Position = [{x}, 0.0, 0.0]\nRotation = [0.0, 0.0, 0.0, 1.0]\nScale = [1.0, 1.0, 1.0]\n"
    )


def tag(body: str) -> str:
    return f'\n[[objects.components]]\nid = "{TAG}"\ntype = "Game.Tag"\n' + body


def lamp() -> prefab.PrefabDocument:
    return prefab.loads(
        "schema_version = 1\n"
        "\n[[objects]]\n" + meta(f'Guid = "{ROOT_LOCAL}"\nName = "Post"\n') + transform()
        + tag('Value = 1\nColour = "red"\n')
        + f'\n[[objects.components]]\nid = "{BODY}"\ntype = "Game.Body"\nMass = 3.5\n'
        + "\n[[objects]]\n"
        + meta(f'Guid = "{CHILD_LOCAL}"\nName = "Bulb"\nParent = "{ROOT_LOCAL}"\n') + transform()
        + "\n[[objects]]\n"
        + meta(f'Guid = "{GRANDCHILD_LOCAL}"\nName = "Filament"\nParent = "{CHILD_LOCAL}"\n')
        + transform(),
        "lamp.prefab",
    )


def level(instance_extra: str = "", *extra: str) -> prefab.PrefabDocument:
    return prefab.loads(
        "schema_version = 1\n"
        "\n[[objects]]\n" + meta(f'Guid = "{LEVEL}"\nName = "Level"\n') + transform()
        + "\n[[objects]]\n"
        + meta(f'Guid = "{FLOOR}"\nName = "Floor"\nParent = "{LEVEL}"\n') + transform()
        + "\n[[objects]]\n"
        + f'prefab = {{ guid = "{LAMP.guid}", path = "{LAMP.path}" }}\n'
        + meta(f'Guid = "{INSTANCE}"\nName = "Lamp_03"\nParent = "{LEVEL}"\n') + transform(5.0)
        + instance_extra
        + "".join(extra),
        "level.prefab",
    )


CARRIER = (
    "\n[[objects]]\n"
    + meta(f'Parent = "{INSTANCE}"\nTarget = "{CHILD_LOCAL}"\nName = "Globe"\n') + transform(2.0)
)

DROP = "\n[[objects]]\n" + meta(f'Parent = "{INSTANCE}"\nTarget = "{CHILD_LOCAL}"\nDropped = true\n')

SECOND_INSTANCE = (
    "\n[[objects]]\n"
    + f'prefab = {{ guid = "{LAMP.guid}", path = "{LAMP.path}" }}\n'
    + meta(f'Guid = "{SECOND}"\nName = "Lamp_04"\nParent = "{LEVEL}"\n') + transform(9.0)
)


class TestTheRoot:
    def test_an_overridden_field_lands_on_the_prefab(self):
        result = apply.apply_to_prefab(level(tag("Value = 7\n")), INSTANCE, lamp())
        assert result.prefab.root().component(TAG).data == {"Value": 7, "Colour": "red"}
        assert result.components == 1

    def test_a_component_only_the_instance_had_is_added(self):
        extra = f'\n[[objects.components]]\nid = "{BODY}"\ntype = "Game.Glow"\nWatts = 40\n'
        source = lamp()
        source.root().components = [
            c for c in source.root().components if c.id != BODY
        ]
        result = apply.apply_to_prefab(level(extra), INSTANCE, source)
        assert result.prefab.root().component(BODY).data == {"Watts": 40}

    def test_a_removed_component_is_deleted_from_the_prefab(self):
        removed = f'\n[[objects.components]]\nid = "{BODY}"\nremoved = true\n'
        result = apply.apply_to_prefab(level(removed), INSTANCE, lamp())
        assert result.prefab.root().component(BODY) is None

    def test_the_roots_placement_never_applies(self):
        """It is where this instance stands in this level. Applying it would move every other
        instance of the prefab to the same spot."""
        result = apply.apply_to_prefab(level(), INSTANCE, lamp())
        assert result.prefab.root().component(TRANSFORM).data["Position"] == [0.0, 0.0, 0.0]

    def test_the_roots_name_never_applies(self):
        result = apply.apply_to_prefab(level(), INSTANCE, lamp())
        assert result.prefab.root().name == "Post"

    def test_an_instance_can_never_remove_its_own_meta(self):
        """Not a rule this module enforces -- the reader does. An object needs a meta to have an
        identity at all, so a second `meta` marked removed is two `meta` entries."""
        removed = f'\n[[objects.components]]\nid = "{META}"\nremoved = true\n'
        with pytest.raises(prefab.PrefabDocumentError, match="twice"):
            level(removed)


class TestCarriers:
    def test_a_childs_transform_does_apply(self):
        result = apply.apply_to_prefab(level("", CARRIER), INSTANCE, lamp())
        child = result.prefab.by_guid()[CHILD_LOCAL]
        assert child.component(TRANSFORM).data["Position"] == [2.0, 0.0, 0.0]
        assert result.children == 1

    def test_a_childs_name_does_apply(self):
        result = apply.apply_to_prefab(level("", CARRIER), INSTANCE, lamp())
        assert result.prefab.by_guid()[CHILD_LOCAL].name == "Globe"

    def test_the_addressing_fields_never_apply(self):
        result = apply.apply_to_prefab(level("", CARRIER), INSTANCE, lamp())
        child = result.prefab.by_guid()[CHILD_LOCAL]
        assert child.parent == ROOT_LOCAL
        assert child.guid == CHILD_LOCAL
        assert child.target is None

    def test_a_dropped_child_takes_its_descendants(self):
        """They would otherwise name a parent the prefab no longer has, which no reader accepts."""
        result = apply.apply_to_prefab(level("", DROP), INSTANCE, lamp())
        assert [entry.name for entry in result.prefab.objects] == ["Post"]
        result.prefab.validate("lamp.prefab")

    def test_removing_the_childs_transform_is_refused_not_applied(self):
        """A carrier CAN hide a prefab child's transform from itself -- the resolver allows it.
        Applying that would leave the prefab holding a child with no placement at all."""
        strip = (
            "\n[[objects]]\n"
            + meta(f'Parent = "{INSTANCE}"\nTarget = "{CHILD_LOCAL}"\n')
            + f'\n[[objects.components]]\nid = "{TRANSFORM}"\nremoved = true\n'
        )
        result = apply.apply_to_prefab(level("", strip), INSTANCE, lamp())
        assert result.prefab.by_guid()[CHILD_LOCAL].component(TRANSFORM) is not None
        assert any("every object needs one" in w for w in result.warnings)

    def test_a_target_the_prefab_does_not_declare_refuses_the_whole_apply(self):
        """That child came out of a prefab nested inside this one, so it exists in no file here.
        All or nothing: a half-applied fold takes some overrides off the instance and leaves
        others, and afterwards nothing says which edit went where."""
        nested = (
            "\n[[objects]]\n"
            + meta(f'Parent = "{INSTANCE}"\nTarget = "dddddddd-0000-4000-8000-000000000009"\n')
            + transform(3.0)
        )
        with pytest.raises(apply.ApplyError, match="nested inside this one"):
            apply.apply_to_prefab(level("", nested), INSTANCE, lamp())


class TestWhatIsLeft:
    def test_the_instance_keeps_only_its_placement(self):
        result = apply.apply_to_prefab(level(tag("Value = 7\n"), CARRIER), INSTANCE, lamp())
        instance = result.remaining.by_guid()[INSTANCE]
        assert [c.id for c in instance.components] == [META, TRANSFORM]
        assert instance.prefab is not None
        assert instance.component(TRANSFORM).data["Position"] == [5.0, 0.0, 0.0]

    def test_the_carriers_are_gone(self):
        result = apply.apply_to_prefab(level("", CARRIER), INSTANCE, lamp())
        assert [e.target for e in result.remaining.objects if e.target] == []

    def test_every_other_object_is_untouched(self):
        result = apply.apply_to_prefab(level(tag("Value = 7\n")), INSTANCE, lamp())
        assert [e.name for e in result.remaining.objects] == ["Level", "Floor", "Lamp_03"]

    def test_the_remaining_document_is_one_the_reader_accepts(self):
        result = apply.apply_to_prefab(level(tag("Value = 7\n"), CARRIER), INSTANCE, lamp())
        prefab.loads(prefab.dumps(result.remaining), "level.prefab")

    def test_the_source_prefab_is_not_mutated(self):
        source = lamp()
        apply.apply_to_prefab(level(tag("Value = 7\n")), INSTANCE, source)
        assert source.root().component(TAG).data == {"Value": 1, "Colour": "red"}


class TestReach:
    def test_the_author_is_told_this_edits_the_asset(self):
        result = apply.apply_to_prefab(level(tag("Value = 7\n")), INSTANCE, lamp())
        assert any("every instance of it changes" in w for w in result.warnings)

    def test_other_instances_in_this_document_are_counted(self):
        result = apply.apply_to_prefab(
            level(tag("Value = 7\n"), SECOND_INSTANCE), INSTANCE, lamp()
        )
        assert any("1 other instance(s)" in w for w in result.warnings)


class TestRefusals:
    def test_a_plain_object_overrides_nothing(self):
        with pytest.raises(apply.ApplyError, match="overrides nothing"):
            apply.apply_to_prefab(level(), FLOOR, lamp())

    def test_an_unknown_identity_is_refused(self):
        with pytest.raises(apply.ApplyError, match="is in this document"):
            apply.apply_to_prefab(level(), "dddddddd-0000-4000-8000-000000000009", lamp())

    def test_text_that_is_not_an_identity_is_refused(self):
        with pytest.raises(apply.ApplyError, match="not an object identity"):
            apply.apply_to_prefab(level(), "Lamp_03", lamp())
