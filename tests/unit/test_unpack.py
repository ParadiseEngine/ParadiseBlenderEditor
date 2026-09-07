"""Breaking a prefab instance's link.

The property that makes this the safe direction, and the one worth defending: unpacking mints
NOTHING. The resolver already gave each displayed child ``uuid5(instance, prefab-local)``, and
unpacking writes exactly that identity, so every reference that resolved before still resolves --
unlike extraction, which mints and therefore breaks references into the subtree.
"""

from __future__ import annotations

import pytest

from paradise_assets.document import prefab, resolve, unpack, well_known
from paradise_assets.document.asset_reference import AssetReference

META = well_known.META_ID
TRANSFORM = well_known.TRANSFORM_ID
TAG = "01b792a0-f12e-4fe9-9867-907ae988b301"

ROOT_LOCAL = "aaaaaaaa-0000-4000-8000-000000000001"
CHILD_LOCAL = "aaaaaaaa-0000-4000-8000-000000000002"

LEVEL = "bbbbbbbb-0000-4000-8000-000000000001"
INSTANCE = "410f381b-fc6e-5a66-a70a-698972a199b5"
FLOOR = "bbbbbbbb-0000-4000-8000-000000000002"
LAMP_POST = "bbbbbbbb-0000-4000-8000-000000000003"

LAMP = AssetReference("cccccccc-0000-4000-8000-000000000001", "prefabs/lamp.prefab")

CHILD = resolve.mint_child_guid(INSTANCE, CHILD_LOCAL)


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


def prefabs(_reference):
    return LAMP_PREFAB


CARRIER = (
    "\n[[objects]]\n"
    + meta(f'Parent = "{INSTANCE}"\nTarget = "{CHILD_LOCAL}"\n') + transform(2.0)
)


def level(*extra: str) -> prefab.PrefabDocument:
    """Level > (Floor, Lamp_03 instance, LampPost) -- the instance in the MIDDLE, so the slot
    the unpacked objects land in is observable."""
    return prefab.loads(
        "schema_version = 1\n"
        "\n[[objects]]\n" + meta(f'Guid = "{LEVEL}"\nName = "Level"\n') + transform()
        + "\n[[objects]]\n"
        + meta(f'Guid = "{FLOOR}"\nName = "Floor"\nParent = "{LEVEL}"\n') + transform()
        + "\n[[objects]]\n"
        + f'prefab = {{ guid = "{LAMP.guid}", path = "{LAMP.path}" }}\n'
        + meta(f'Guid = "{INSTANCE}"\nName = "Lamp_03"\nParent = "{LEVEL}"\n') + transform(5.0)
        + tag("Value = 7\n")
        + "\n[[objects]]\n"
        + meta(f'Guid = "{LAMP_POST}"\nName = "Post"\nParent = "{LEVEL}"\n') + transform()
        + "".join(extra),
        "level.prefab",
    )


def names(document) -> list:
    return [entry.name for entry in document.objects]


class TestWhatComesOut:
    def test_the_instance_becomes_the_objects_it_was_showing(self):
        result = unpack.unpack(level(), INSTANCE, prefabs)
        assert result.objects == 2
        assert names(result.document) == ["Level", "Floor", "Lamp_03", "Bulb", "Post"]

    def test_nothing_still_instances_the_prefab(self):
        result = unpack.unpack(level(), INSTANCE, prefabs)
        assert all(entry.prefab is None for entry in result.document.objects)

    def test_the_root_keeps_the_instance_identity_name_and_parent(self):
        """So every reference to the instance still names it, and it hangs where it hung."""
        root = unpack.unpack(level(), INSTANCE, prefabs).document.by_guid()[INSTANCE]
        assert (root.name, root.parent) == ("Lamp_03", LEVEL)
        assert root.component(TRANSFORM).data["Position"] == [5.0, 0.0, 0.0]

    def test_a_child_keeps_the_identity_the_resolver_gave_it(self):
        child = unpack.unpack(level(), INSTANCE, prefabs).document.by_guid()[CHILD]
        assert (child.name, child.parent) == ("Bulb", INSTANCE)

    def test_the_overrides_are_baked_in(self):
        """They were the truth on screen; unpacking must not quietly revert to the prefab."""
        result = unpack.unpack(level(CARRIER), INSTANCE, prefabs)
        assert result.document.by_guid()[INSTANCE].component(TAG).data == {
            "Value": 7, "Colour": "red"
        }
        assert result.document.by_guid()[CHILD].component(TRANSFORM).data["Position"] == [
            2.0, 0.0, 0.0
        ]

    def test_the_carriers_are_consumed(self):
        result = unpack.unpack(level(CARRIER), INSTANCE, prefabs)
        assert result.carriers == 1
        assert [entry.target for entry in result.document.objects] == [None] * 5

    def test_the_result_is_a_document_the_reader_accepts(self):
        result = unpack.unpack(level(CARRIER), INSTANCE, prefabs)
        prefab.loads(prefab.dumps(result.document), "unpacked.prefab")

    def test_unpacking_and_resolving_leave_the_same_document(self):
        """The whole claim: what the author saw is what they now own."""
        before = resolve.resolve(level(CARRIER), prefabs).document
        after = resolve.resolve(unpack.unpack(level(CARRIER), INSTANCE, prefabs).document, prefabs)
        assert prefab.dumps(before) == prefab.dumps(after.document)


class TestNesting:
    def test_a_prefab_nested_inside_is_flattened_too(self):
        """Resolution flattens it, so the author was never shown a half-way state and unpacking
        must not invent one."""
        inner_local = "aaaaaaaa-0000-4000-8000-000000000003"
        bulb = AssetReference("cccccccc-0000-4000-8000-000000000002", "prefabs/bulb.prefab")

        nested = prefab.loads(
            "schema_version = 1\n"
            "\n[[objects]]\n" + meta(f'Guid = "{ROOT_LOCAL}"\nName = "Post"\n') + transform()
            + "\n[[objects]]\n"
            + f'prefab = {{ guid = "{bulb.guid}", path = "{bulb.path}" }}\n'
            + meta(f'Guid = "{CHILD_LOCAL}"\nName = "Bulb"\nParent = "{ROOT_LOCAL}"\n') + transform(),
            "lamp.prefab",
        )
        inner = prefab.loads(
            "schema_version = 1\n"
            "\n[[objects]]\n" + meta(f'Guid = "{ROOT_LOCAL}"\nName = "Glass"\n') + transform()
            + "\n[[objects]]\n"
            + meta(f'Guid = "{inner_local}"\nName = "Filament"\nParent = "{ROOT_LOCAL}"\n')
            + transform(),
            "bulb.prefab",
        )

        def nesting(reference):
            return nested if reference.path == LAMP.path else inner

        result = unpack.unpack(level(), INSTANCE, nesting)
        assert result.objects == 3
        assert names(result.document) == ["Level", "Floor", "Lamp_03", "Bulb", "Filament", "Post"]
        assert all(entry.prefab is None for entry in result.document.objects)


class TestRefusals:
    def test_a_plain_object_has_no_link_to_break(self):
        with pytest.raises(unpack.UnpackError, match="does not instance a prefab"):
            unpack.unpack(level(), FLOOR, prefabs)

    def test_an_unknown_identity_is_refused(self):
        with pytest.raises(unpack.UnpackError, match="is in this document"):
            unpack.unpack(level(), "dddddddd-0000-4000-8000-000000000009", prefabs)

    def test_text_that_is_not_an_identity_is_refused(self):
        with pytest.raises(unpack.UnpackError, match="not an object identity"):
            unpack.unpack(level(), "Lamp_03", prefabs)

    def test_an_override_carrier_is_refused(self):
        """A carrier may carry a Guid of its own; addressing it is still addressing an override
        rather than an instance."""
        identified = (
            "\n[[objects]]\n"
            + meta(
                f'Guid = "dddddddd-0000-4000-8000-000000000001"\n'
                f'Parent = "{INSTANCE}"\nTarget = "{CHILD_LOCAL}"\n'
            )
            + transform(2.0)
        )
        with pytest.raises(unpack.UnpackError, match="override"):
            unpack.unpack(
                level(identified), "dddddddd-0000-4000-8000-000000000001", prefabs
            )

    def test_a_resolved_child_is_not_in_the_file_at_all(self):
        """Its identity is minted, so from here it simply is not there; the operator, which can
        see the instance it belongs to, is what turns this into a useful sentence."""
        with pytest.raises(unpack.UnpackError, match="is in this document"):
            unpack.unpack(level(CARRIER), CHILD, prefabs)

    def test_a_prefab_that_cannot_be_read_refuses_rather_than_deleting_the_subtree(self):
        with pytest.raises(unpack.UnpackError, match="nothing to unpack it into"):
            unpack.unpack(level(), INSTANCE, lambda _reference: None)
