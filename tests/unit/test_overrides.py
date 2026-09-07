"""What an instance overrides, and what the prefab alone would have said.

The two things the authoring side needs that resolution does not report: a BASELINE to diff the
shown value against, and the prefab-LOCAL address of each resolved child, since a carrier spells
that and ``uuid5`` does not invert.
"""

from __future__ import annotations

from paradise_assets.document import overrides, prefab, resolve, well_known
from paradise_assets.document.asset_reference import AssetReference

META = well_known.META_ID
TRANSFORM = well_known.TRANSFORM_ID
TAG = "01b792a0-f12e-4fe9-9867-907ae988b301"

ROOT_LOCAL = "aaaaaaaa-0000-4000-8000-000000000001"
CHILD_LOCAL = "aaaaaaaa-0000-4000-8000-000000000002"
INNER_LOCAL = "aaaaaaaa-0000-4000-8000-000000000003"

LEVEL = "bbbbbbbb-0000-4000-8000-000000000001"
INSTANCE = "410f381b-fc6e-5a66-a70a-698972a199b5"
PLAIN = "bbbbbbbb-0000-4000-8000-000000000002"

LAMP = AssetReference("cccccccc-0000-4000-8000-000000000001", "prefabs/lamp.prefab")
BULB = AssetReference("cccccccc-0000-4000-8000-000000000002", "prefabs/bulb.prefab")


def meta(body: str) -> str:
    return f'\n[[objects.components]]\nid = "{META}"\ntype = "meta"\n' + body


def transform(x: float = 0.0) -> str:
    return (
        f'\n[[objects.components]]\nid = "{TRANSFORM}"\ntype = "transform"\n'
        f"Position = [{x}, 0.0, 0.0]\nRotation = [0.0, 0.0, 0.0, 1.0]\nScale = [1.0, 1.0, 1.0]\n"
    )


def tag(body: str) -> str:
    return f'\n[[objects.components]]\nid = "{TAG}"\ntype = "Game.Tag"\n' + body


LAMP_PREFAB = prefab.loads(
    "schema_version = 1\n"
    "\n[[objects]]\n" + meta(f'Guid = "{ROOT_LOCAL}"\nName = "Post"\n') + transform()
    + tag('Value = 1\nColour = "red"\n')
    + "\n[[objects]]\n"
    + meta(f'Guid = "{CHILD_LOCAL}"\nName = "Bulb"\nParent = "{ROOT_LOCAL}"\n') + transform(),
    "lamp.prefab",
)


def prefabs(reference):
    return LAMP_PREFAB if reference.path == LAMP.path else None


def level(*extra: str) -> prefab.PrefabDocument:
    """A level holding one plain object and one instance that overrides ``Tag.Value``."""
    return prefab.loads(
        "schema_version = 1\n"
        "\n[[objects]]\n" + meta(f'Guid = "{LEVEL}"\nName = "Level"\n') + transform()
        + "\n[[objects]]\n"
        + f'prefab = {{ guid = "{LAMP.guid}", path = "{LAMP.path}" }}\n'
        + meta(f'Guid = "{INSTANCE}"\nName = "Lamp_03"\nParent = "{LEVEL}"\n') + transform(5.0)
        + tag("Value = 7\n")
        + "\n[[objects]]\n"
        + meta(f'Guid = "{PLAIN}"\nName = "Floor"\nParent = "{LEVEL}"\n') + transform()
        + "".join(extra),
        "level.prefab",
    )


CARRIER = (
    "\n[[objects]]\n"
    + meta(f'Parent = "{INSTANCE}"\nTarget = "{CHILD_LOCAL}"\n') + transform(2.0)
)


def shown(document) -> dict:
    return resolve.resolve(document, prefabs).document.by_guid()


class TestBaseline:
    def test_an_overridden_field_comes_back_as_the_prefab_wrote_it(self):
        base = overrides.baseline(level(), prefabs)
        assert base[INSTANCE].component(TAG).data == {"Value": 1, "Colour": "red"}
        assert shown(level())[INSTANCE].component(TAG).data == {"Value": 7, "Colour": "red"}

    def test_a_carrier_is_stripped_too(self):
        base = overrides.baseline(level(CARRIER), prefabs)
        child = resolve.mint_child_guid(INSTANCE, CHILD_LOCAL)
        assert base[child].component(TRANSFORM).data["Position"] == [0.0, 0.0, 0.0]
        assert shown(level(CARRIER))[child].component(TRANSFORM).data["Position"] == [2.0, 0.0, 0.0]

    def test_the_instances_own_placement_survives_stripping(self):
        """The instance's transform is the LEVEL's, not an override, so a baseline keeps it --
        or every instance would diff as having moved."""
        base = overrides.baseline(level(), prefabs)
        assert base[INSTANCE].component(TRANSFORM).data["Position"] == [5.0, 0.0, 0.0]

    def test_a_plain_object_is_its_own_baseline(self):
        base = overrides.baseline(level(), prefabs)
        assert base[PLAIN].name == "Floor"
        assert overrides.differs(
            base[PLAIN].component(TRANSFORM).data,
            shown(level())[PLAIN].component(TRANSFORM).data,
        ) == set()


class TestLocals:
    def test_a_resolved_child_carries_the_address_a_carrier_spells(self):
        found = overrides.locals_of(level(), prefabs)
        minted = resolve.mint_child_guid(INSTANCE, CHILD_LOCAL)
        assert found[minted] == overrides.LocalRef(INSTANCE, CHILD_LOCAL, True)

    def test_the_instance_root_is_not_addressed_by_a_carrier(self):
        """The root is overridden on the instance entry itself; a carrier for it would be a
        second way to say the same thing, and the resolver reads only the first."""
        assert INSTANCE not in overrides.locals_of(level(), prefabs)

    def test_a_child_of_a_nested_prefab_is_addressable_but_not_ours(self):
        nested = prefab.loads(
            "schema_version = 1\n"
            "\n[[objects]]\n" + meta(f'Guid = "{ROOT_LOCAL}"\nName = "Post"\n') + transform()
            + "\n[[objects]]\n"
            + f'prefab = {{ guid = "{BULB.guid}", path = "{BULB.path}" }}\n'
            + meta(f'Guid = "{CHILD_LOCAL}"\nName = "Bulb"\nParent = "{ROOT_LOCAL}"\n') + transform(),
            "lamp.prefab",
        )
        bulb = prefab.loads(
            "schema_version = 1\n"
            "\n[[objects]]\n" + meta(f'Guid = "{ROOT_LOCAL}"\nName = "Glass"\n') + transform()
            + "\n[[objects]]\n"
            + meta(f'Guid = "{INNER_LOCAL}"\nName = "Filament"\nParent = "{ROOT_LOCAL}"\n')
            + transform(),
            "bulb.prefab",
        )

        def nesting(reference):
            return nested if reference.path == LAMP.path else bulb

        found = overrides.locals_of(level(), nesting)
        deep_local = resolve.mint_child_guid(CHILD_LOCAL, INNER_LOCAL)
        deep = resolve.mint_child_guid(INSTANCE, deep_local)

        assert found[resolve.mint_child_guid(INSTANCE, CHILD_LOCAL)].own is True
        assert found[deep] == overrides.LocalRef(INSTANCE, deep_local, False)

    def test_an_unreadable_prefab_contributes_nothing(self):
        assert overrides.locals_of(level(), lambda _reference: None) == {}


class TestCarriers:
    def test_a_carrier_is_found_by_the_child_it_addresses(self):
        document = level(CARRIER)
        assert set(overrides.carriers_of(document, INSTANCE)) == {CHILD_LOCAL}
        assert overrides.carrier_for(document, INSTANCE, CHILD_LOCAL) is not None
        assert overrides.carrier_for(document, INSTANCE, ROOT_LOCAL) is None

    def test_a_new_carrier_has_no_identity_of_its_own(self):
        made = overrides.new_carrier(INSTANCE, CHILD_LOCAL)
        assert made.guid is None
        assert (made.parent, made.target) == (INSTANCE, CHILD_LOCAL)

    def test_a_carrier_that_says_nothing_is_empty(self):
        made = overrides.new_carrier(INSTANCE, CHILD_LOCAL)
        assert overrides.is_empty(made)

        made.components.append(prefab.PrefabComponent(TAG, "Game.Tag", {"Value": 2}))
        assert not overrides.is_empty(made)

    def test_a_dropping_carrier_is_never_empty(self):
        """It carries nothing but the drop IS what it says."""
        made = overrides.new_carrier(INSTANCE, CHILD_LOCAL)
        made.meta.data[well_known.DROPPED] = True
        assert not overrides.is_empty(made)

    def test_a_meta_field_the_game_added_keeps_a_carrier_alive(self):
        made = overrides.new_carrier(INSTANCE, CHILD_LOCAL)
        made.meta.data["Layer"] = "water"
        assert not overrides.is_empty(made)


class TestDiffers:
    def test_only_the_changed_field_is_named(self):
        assert overrides.differs({"a": 1, "b": 2}, {"a": 1, "b": 3}) == {"b"}

    def test_a_field_only_one_side_has_counts(self):
        assert overrides.differs({"a": 1}, {"a": 1, "b": 2}) == {"b"}
        assert overrides.differs({"a": 1, "b": 2}, {"a": 1}) == {"b"}

    def test_a_replaced_sub_table_is_named_at_the_top(self):
        """`resolve._merge_data` is shallow, so the overridden field IS the top-level one."""
        assert overrides.differs({"a": {"x": 1}}, {"a": {"x": 2}}) == {"a"}
