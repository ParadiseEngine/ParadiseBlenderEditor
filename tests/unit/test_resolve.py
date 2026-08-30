"""The prefab resolver.

These mirror ``Paradise.Assets.Pipeline.Test/PrefabResolverTests.cs`` case for case and value for
value. Where a number appears here it appears there too -- that is the whole point: two
implementations of one set of normative rules, checked against the same fixtures, because a
divergence would otherwise surface much later as two tools disagreeing about a document.
"""

from __future__ import annotations

from paradise_assets.document import prefab, resolve, well_known
from paradise_assets.document.asset_reference import AssetReference
from paradise_assets.document.prefab import PrefabComponent, PrefabDocument, PrefabDocumentError, PrefabObject

ROOT_LOCAL = "aaaaaaaa-0000-4000-8000-000000000001"
CHILD_LOCAL = "aaaaaaaa-0000-4000-8000-000000000002"
INSTANCE_GUID = "410f381b-fc6e-5a66-a70a-698972a199b5"
MESH_ID = "edee8bd8-9321-47db-819d-9bdadf010be4"
MATERIALS_ID = "bdc4fc87-d7b4-41f1-bc90-fc827005adfc"
TAG_ID = "01b792a0-f12e-4fe9-9867-907ae988b301"

PREFAB_REF = AssetReference("5f2a1111-2222-4333-8444-555555555555", "prefabs/lamp.prefab")


def single_object_prefab() -> prefab.PrefabDocument:
    root = PrefabObject.with_meta(ROOT_LOCAL, "Post")
    root.components.append(
        PrefabComponent(
            well_known.TRANSFORM_ID,
            well_known.TRANSFORM_TYPE,
            {well_known.POSITION: [0.0, 0.0, 0.0], well_known.SCALE: [1.0, 1.0, 1.0]},
        )
    )
    root.components.append(PrefabComponent(MESH_ID, "ObstacleMesh", {"Mesh": "Models/unit_box.glb"}))
    root.components.append(PrefabComponent(TAG_ID, "ObstacleTag"))
    return validated(PrefabDocument([root]), "lamp.prefab")


def two_object_prefab() -> prefab.PrefabDocument:
    document = single_object_prefab()
    child = PrefabObject.with_meta(CHILD_LOCAL, "Bulb", ROOT_LOCAL)
    child.components.append(PrefabComponent(MATERIALS_ID, "Materials", {"Slots": ["materials/warm.toml"]}))
    document.objects.append(child)
    return validated(document, "lamp.prefab")


def instance(*extra: PrefabComponent) -> PrefabObject:
    obj = PrefabObject.with_meta(INSTANCE_GUID, "Lamp_03")
    obj.prefab = PREFAB_REF
    obj.components.extend(extra)
    return obj


def validated(document: prefab.PrefabDocument, source: str) -> prefab.PrefabDocument:
    """The document, having checked it obeys the single-root rule.

    Fixtures are built object by object rather than parsed, so they skip the reader where that rule
    is normally enforced. Running it here keeps a fixture from being something no reader would ever
    hand the resolver.
    """
    document.validate(source)
    return document


def expand(source: prefab.PrefabDocument, *objects: PrefabObject) -> resolve.ResolveResult:
    return resolve.resolve(PrefabDocument(list(objects)), lambda _: source)


OUTER_REF = AssetReference("5f2a6666-7777-4888-8999-aaaaaaaaaaaa", "prefabs/fitting.prefab")
OUTER_ROOT_LOCAL = "c0ffee00-0000-4000-8000-000000000001"
OUTER_INNER_LOCAL = "c0ffee00-0000-4000-8000-000000000002"


def outer_prefab() -> prefab.PrefabDocument:
    """A prefab whose child instantiates ``prefabs/lamp.prefab``."""
    inner = PrefabObject.with_meta(OUTER_INNER_LOCAL, "Post", OUTER_ROOT_LOCAL)
    inner.prefab = PREFAB_REF
    document = PrefabDocument([PrefabObject.with_meta(OUTER_ROOT_LOCAL, "Fitting"), inner])
    return validated(document, "fitting.prefab")


def self_referencing_prefab() -> prefab.PrefabDocument:
    """A prefab that reaches itself through its own child."""
    document = single_object_prefab()
    child = PrefabObject.with_meta(CHILD_LOCAL, "Inner", ROOT_LOCAL)
    child.prefab = PREFAB_REF
    document.objects.append(child)
    return validated(document, "prefabs/lamp.prefab")


def outer_instance() -> PrefabObject:
    obj = PrefabObject.with_meta(INSTANCE_GUID, "Fitting_01")
    obj.prefab = OUTER_REF
    return obj


def two_level_lookup(reference: AssetReference) -> prefab.PrefabDocument:
    return single_object_prefab() if reference.path == PREFAB_REF.path else outer_prefab()


class TestSingleObject:
    def test_an_instance_becomes_the_root_carrying_its_own_identity(self):
        result = expand(single_object_prefab(), instance())

        assert result.errors == []
        assert len(result.document.objects) == 1
        resolved = result.document.objects[0]
        assert resolved.guid == INSTANCE_GUID
        assert resolved.name == "Lamp_03"
        assert resolved.prefab is None  # flattened: nothing downstream sees prefabs

    def test_unmentioned_components_are_inherited(self):
        resolved = expand(single_object_prefab(), instance()).document.objects[0]

        assert resolved.component(MESH_ID).data["Mesh"] == "Models/unit_box.glb"
        assert resolved.component(TAG_ID) is not None

    def test_a_repeated_component_is_overridden_field_by_field(self):
        # Scale given, Position not -- so Position must survive from the prefab, or every instance
        # would have to restate every field it did not want to change.
        obj = instance(
            PrefabComponent(well_known.TRANSFORM_ID, well_known.TRANSFORM_TYPE, {well_known.SCALE: [1.0, 0.08, 4.0]})
        )

        transform = expand(single_object_prefab(), obj).document.objects[0].component(well_known.TRANSFORM_ID)

        assert transform.data[well_known.SCALE] == [1.0, 0.08, 4.0]
        assert well_known.POSITION in transform.data

    def test_a_component_only_the_instance_has_is_added(self):
        obj = instance(PrefabComponent(MATERIALS_ID, "Materials", {"Slots": ["materials/red.toml"]}))

        assert expand(single_object_prefab(), obj).document.objects[0].component(MATERIALS_ID) is not None

    def test_a_removed_component_is_dropped(self):
        obj = instance(PrefabComponent(TAG_ID, removed=True))

        resolved = expand(single_object_prefab(), obj).document.objects[0]

        assert resolved.component(TAG_ID) is None
        assert resolved.component(MESH_ID) is not None

    def test_removing_a_component_the_prefab_does_not_have_is_an_error(self):
        result = expand(single_object_prefab(), instance(PrefabComponent(MATERIALS_ID, removed=True)))

        assert len(result.errors) == 1
        assert "does not have" in result.errors[0]

    def test_component_order_is_prefab_order_then_instance_additions(self):
        obj = instance(PrefabComponent(MATERIALS_ID, "Materials"))

        ids = [c.id for c in expand(single_object_prefab(), obj).document.objects[0].components]

        assert ids == [well_known.META_ID, well_known.TRANSFORM_ID, MESH_ID, TAG_ID, MATERIALS_ID]

    def test_an_instance_can_be_parented_though_the_prefab_root_has_no_parent(self):
        # The rule a naive "unknown field" check would forbid, and the commonest edit there is.
        holder = PrefabObject.with_meta(CHILD_LOCAL, "Holder")
        obj = instance()
        obj.components[0] = PrefabComponent(
            well_known.META_ID,
            well_known.META_TYPE,
            {well_known.GUID: INSTANCE_GUID, well_known.NAME: "Lamp_03", well_known.PARENT: CHILD_LOCAL},
        )

        result = expand(single_object_prefab(), holder, obj)

        assert result.errors == []
        assert result.document.objects[1].parent == CHILD_LOCAL


class TestMultipleObjects:
    def test_children_follow_their_instance_in_prefab_document_order(self):
        result = expand(two_object_prefab(), instance())

        assert [o.name for o in result.document.objects] == ["Lamp_03", "Bulb"]

    def test_a_child_gets_a_minted_identity_parented_to_the_instance(self):
        child = expand(two_object_prefab(), instance()).document.objects[1]

        assert child.guid == resolve.mint_child_guid(INSTANCE_GUID, CHILD_LOCAL)
        assert child.parent == INSTANCE_GUID

    def test_two_instances_give_their_children_different_identities(self):
        second = PrefabObject.with_meta("7c2e9a41-1111-4222-8333-444444444444", "Lamp_04")
        second.prefab = PREFAB_REF

        result = expand(two_object_prefab(), instance(), second)

        assert result.document.objects[1].guid != result.document.objects[3].guid

    def test_minting_matches_the_value_the_csharp_resolver_produces(self):
        # THE cross-language fixture. C# hashes the guid's canonical TEXT for exactly this reason:
        # .NET's Guid.ToByteArray is mixed-endian and Python's UUID.bytes is big-endian, so
        # hashing raw bytes would give two different answers and nothing would catch it.
        assert resolve.mint_child_guid(INSTANCE_GUID, CHILD_LOCAL) == "6a8f7f6a-5cf4-59f3-ae75-6717b3ae43e3"

    def test_a_carrier_overrides_a_child_and_occupies_no_slot(self):
        carrier = PrefabObject(
            components=[
                PrefabComponent(
                    well_known.META_ID,
                    well_known.META_TYPE,
                    {well_known.PARENT: INSTANCE_GUID, well_known.TARGET: CHILD_LOCAL},
                ),
                PrefabComponent(MATERIALS_ID, "Materials", {"Slots": ["materials/dead.toml"]}),
            ]
        )

        result = expand(two_object_prefab(), instance(), carrier)

        assert result.errors == []
        assert len(result.document.objects) == 2  # carrier consumed
        assert result.document.objects[1].component(MATERIALS_ID).data["Slots"] == ["materials/dead.toml"]

    def test_a_dropped_child_is_removed(self):
        carrier = PrefabObject(
            components=[
                PrefabComponent(
                    well_known.META_ID,
                    well_known.META_TYPE,
                    {
                        well_known.PARENT: INSTANCE_GUID,
                        well_known.TARGET: CHILD_LOCAL,
                        well_known.DROPPED: True,
                    },
                )
            ]
        )

        result = expand(two_object_prefab(), instance(), carrier)

        assert [o.name for o in result.document.objects] == ["Lamp_03"]

    def test_a_carrier_targeting_nothing_is_an_error(self):
        carrier = PrefabObject(
            components=[
                PrefabComponent(
                    well_known.META_ID,
                    well_known.META_TYPE,
                    {well_known.PARENT: INSTANCE_GUID, well_known.TARGET: "99999999-8888-4777-8666-555555555555"},
                )
            ]
        )

        result = expand(two_object_prefab(), instance(), carrier)

        assert len(result.errors) == 1
        assert "does not contain" in result.errors[0]


class TestValidation:
    def rejects(self, document, source="x.prefab") -> str:
        try:
            document.validate(source)
        except PrefabDocumentError as error:
            return str(error)
        raise AssertionError("expected the prefab to be rejected")

    def test_two_roots_are_refused(self):
        document = single_object_prefab()
        document.objects.append(PrefabObject.with_meta(CHILD_LOCAL, "Loose"))

        assert "2 root objects" in self.rejects(document)

    def test_an_empty_document_is_refused(self):
        assert "no objects" in self.rejects(PrefabDocument())


class TestNesting:
    """Mirrors ``PrefabResolverTests`` in C#, case for case."""

    def test_a_prefab_may_instantiate_another_prefab(self):
        # fitting.prefab's own child instantiates lamp.prefab, so ONE instance of fitting has to
        # expand two levels. A level is a document holding instances, so this is also every level.
        result = resolve.resolve(PrefabDocument([outer_instance()]), two_level_lookup)

        assert result.errors == []
        assert [o.name for o in result.document.objects] == ["Fitting_01", "Post"]

    def test_a_nested_instance_mints_identities_that_survive_two_levels(self):
        result = resolve.resolve(PrefabDocument([outer_instance()]), two_level_lookup)

        guids = [o.guid for o in result.document.objects]
        assert len(set(guids)) == len(guids)
        assert ROOT_LOCAL not in guids
        assert OUTER_INNER_LOCAL not in guids

    def test_a_prefab_cycle_is_reported_rather_than_recursed(self):
        # Without the stack this recurses until the interpreter gives up, which tells an author
        # nothing; the error names the chain that closed the loop.
        result = resolve.resolve(PrefabDocument([instance()]), lambda _: self_referencing_prefab())

        assert len(result.errors) == 1
        assert "form a cycle" in result.errors[0]
        assert "lamp.prefab" in result.errors[0]

    def test_a_plain_object_passes_through_untouched(self):
        plain = PrefabObject.with_meta(CHILD_LOCAL, "Hand placed")

        result = expand(single_object_prefab(), plain)

        assert result.expanded == 0
        assert result.document.objects[0] is plain
