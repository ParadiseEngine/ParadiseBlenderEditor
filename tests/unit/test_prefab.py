"""The prefab resolver.

These mirror ``Paradise.Assets.Pipeline.Test/PrefabResolverTests.cs`` case for case and value for
value. Where a number appears here it appears there too -- that is the whole point: two
implementations of one set of normative rules, checked against the same fixtures, because a
divergence would otherwise surface much later as two tools disagreeing about a document.
"""

from __future__ import annotations

from paradise_assets.document import prefab, well_known
from paradise_assets.document.asset_reference import AssetReference
from paradise_assets.document.scene import SceneComponent, SceneDocument, SceneDocumentError, SceneObject

ROOT_LOCAL = "aaaaaaaa-0000-4000-8000-000000000001"
CHILD_LOCAL = "aaaaaaaa-0000-4000-8000-000000000002"
INSTANCE_GUID = "410f381b-fc6e-5a66-a70a-698972a199b5"
MESH_ID = "edee8bd8-9321-47db-819d-9bdadf010be4"
MATERIALS_ID = "bdc4fc87-d7b4-41f1-bc90-fc827005adfc"
TAG_ID = "01b792a0-f12e-4fe9-9867-907ae988b301"

PREFAB_REF = AssetReference("5f2a1111-2222-4333-8444-555555555555", "prefabs/lamp.prefab")


def single_object_prefab() -> prefab.PrefabDocument:
    root = SceneObject.with_meta(ROOT_LOCAL, "Post")
    root.components.append(
        SceneComponent(
            well_known.TRANSFORM_ID,
            well_known.TRANSFORM_TYPE,
            {well_known.POSITION: [0.0, 0.0, 0.0], well_known.SCALE: [1.0, 1.0, 1.0]},
        )
    )
    root.components.append(SceneComponent(MESH_ID, "ObstacleMesh", {"Mesh": "Models/unit_box.glb"}))
    root.components.append(SceneComponent(TAG_ID, "ObstacleTag"))
    return prefab.validate(SceneDocument([root]), "lamp.prefab")


def two_object_prefab() -> prefab.PrefabDocument:
    document = single_object_prefab().document
    child = SceneObject.with_meta(CHILD_LOCAL, "Bulb", ROOT_LOCAL)
    child.components.append(SceneComponent(MATERIALS_ID, "Materials", {"Slots": ["materials/warm.toml"]}))
    document.objects.append(child)
    return prefab.validate(document, "lamp.prefab")


def instance(*extra: SceneComponent) -> SceneObject:
    obj = SceneObject.with_meta(INSTANCE_GUID, "Lamp_03")
    obj.prefab = PREFAB_REF
    obj.components.extend(extra)
    return obj


def resolve(source: prefab.PrefabDocument, *objects: SceneObject) -> prefab.ResolveResult:
    return prefab.resolve(SceneDocument(list(objects)), lambda _: source)


class TestSingleObject:
    def test_an_instance_becomes_the_root_carrying_its_own_identity(self):
        result = resolve(single_object_prefab(), instance())

        assert result.errors == []
        assert len(result.document.objects) == 1
        resolved = result.document.objects[0]
        assert resolved.guid == INSTANCE_GUID
        assert resolved.name == "Lamp_03"
        assert resolved.prefab is None  # flattened: nothing downstream sees prefabs

    def test_unmentioned_components_are_inherited(self):
        resolved = resolve(single_object_prefab(), instance()).document.objects[0]

        assert resolved.component(MESH_ID).data["Mesh"] == "Models/unit_box.glb"
        assert resolved.component(TAG_ID) is not None

    def test_a_repeated_component_is_overridden_field_by_field(self):
        # Scale given, Position not -- so Position must survive from the prefab, or every instance
        # would have to restate every field it did not want to change.
        obj = instance(
            SceneComponent(well_known.TRANSFORM_ID, well_known.TRANSFORM_TYPE, {well_known.SCALE: [1.0, 0.08, 4.0]})
        )

        transform = resolve(single_object_prefab(), obj).document.objects[0].component(well_known.TRANSFORM_ID)

        assert transform.data[well_known.SCALE] == [1.0, 0.08, 4.0]
        assert well_known.POSITION in transform.data

    def test_a_component_only_the_instance_has_is_added(self):
        obj = instance(SceneComponent(MATERIALS_ID, "Materials", {"Slots": ["materials/red.toml"]}))

        assert resolve(single_object_prefab(), obj).document.objects[0].component(MATERIALS_ID) is not None

    def test_a_removed_component_is_dropped(self):
        obj = instance(SceneComponent(TAG_ID, removed=True))

        resolved = resolve(single_object_prefab(), obj).document.objects[0]

        assert resolved.component(TAG_ID) is None
        assert resolved.component(MESH_ID) is not None

    def test_removing_a_component_the_prefab_does_not_have_is_an_error(self):
        result = resolve(single_object_prefab(), instance(SceneComponent(MATERIALS_ID, removed=True)))

        assert len(result.errors) == 1
        assert "does not have" in result.errors[0]

    def test_component_order_is_prefab_order_then_instance_additions(self):
        obj = instance(SceneComponent(MATERIALS_ID, "Materials"))

        ids = [c.id for c in resolve(single_object_prefab(), obj).document.objects[0].components]

        assert ids == [well_known.META_ID, well_known.TRANSFORM_ID, MESH_ID, TAG_ID, MATERIALS_ID]

    def test_an_instance_can_be_parented_though_the_prefab_root_has_no_parent(self):
        # The rule a naive "unknown field" check would forbid, and the commonest edit there is.
        holder = SceneObject.with_meta(CHILD_LOCAL, "Holder")
        obj = instance()
        obj.components[0] = SceneComponent(
            well_known.META_ID,
            well_known.META_TYPE,
            {well_known.GUID: INSTANCE_GUID, well_known.NAME: "Lamp_03", well_known.PARENT: CHILD_LOCAL},
        )

        result = resolve(single_object_prefab(), holder, obj)

        assert result.errors == []
        assert result.document.objects[1].parent == CHILD_LOCAL


class TestMultipleObjects:
    def test_children_follow_their_instance_in_prefab_document_order(self):
        result = resolve(two_object_prefab(), instance())

        assert [o.name for o in result.document.objects] == ["Lamp_03", "Bulb"]

    def test_a_child_gets_a_minted_identity_parented_to_the_instance(self):
        child = resolve(two_object_prefab(), instance()).document.objects[1]

        assert child.guid == prefab.mint_child_guid(INSTANCE_GUID, CHILD_LOCAL)
        assert child.parent == INSTANCE_GUID

    def test_two_instances_give_their_children_different_identities(self):
        second = SceneObject.with_meta("7c2e9a41-1111-4222-8333-444444444444", "Lamp_04")
        second.prefab = PREFAB_REF

        result = resolve(two_object_prefab(), instance(), second)

        assert result.document.objects[1].guid != result.document.objects[3].guid

    def test_minting_matches_the_value_the_csharp_resolver_produces(self):
        # THE cross-language fixture. C# hashes the guid's canonical TEXT for exactly this reason:
        # .NET's Guid.ToByteArray is mixed-endian and Python's UUID.bytes is big-endian, so
        # hashing raw bytes would give two different answers and nothing would catch it.
        assert prefab.mint_child_guid(INSTANCE_GUID, CHILD_LOCAL) == "6a8f7f6a-5cf4-59f3-ae75-6717b3ae43e3"

    def test_a_carrier_overrides_a_child_and_occupies_no_slot(self):
        carrier = SceneObject(
            components=[
                SceneComponent(
                    well_known.META_ID,
                    well_known.META_TYPE,
                    {well_known.PARENT: INSTANCE_GUID, well_known.TARGET: CHILD_LOCAL},
                ),
                SceneComponent(MATERIALS_ID, "Materials", {"Slots": ["materials/dead.toml"]}),
            ]
        )

        result = resolve(two_object_prefab(), instance(), carrier)

        assert result.errors == []
        assert len(result.document.objects) == 2  # carrier consumed
        assert result.document.objects[1].component(MATERIALS_ID).data["Slots"] == ["materials/dead.toml"]

    def test_a_dropped_child_is_removed(self):
        carrier = SceneObject(
            components=[
                SceneComponent(
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

        result = resolve(two_object_prefab(), instance(), carrier)

        assert [o.name for o in result.document.objects] == ["Lamp_03"]

    def test_a_carrier_targeting_nothing_is_an_error(self):
        carrier = SceneObject(
            components=[
                SceneComponent(
                    well_known.META_ID,
                    well_known.META_TYPE,
                    {well_known.PARENT: INSTANCE_GUID, well_known.TARGET: "99999999-8888-4777-8666-555555555555"},
                )
            ]
        )

        result = resolve(two_object_prefab(), instance(), carrier)

        assert len(result.errors) == 1
        assert "does not contain" in result.errors[0]


class TestValidation:
    def rejects(self, document, source="x.prefab") -> str:
        try:
            prefab.validate(document, source)
        except SceneDocumentError as error:
            return str(error)
        raise AssertionError("expected the prefab to be rejected")

    def test_two_roots_are_refused(self):
        document = single_object_prefab().document
        document.objects.append(SceneObject.with_meta(CHILD_LOCAL, "Loose"))

        assert "2 root objects" in self.rejects(document)

    def test_a_nested_prefab_is_refused_for_now(self):
        document = single_object_prefab().document
        document.objects[0].prefab = PREFAB_REF

        assert "not supported yet" in self.rejects(document)

    def test_an_empty_prefab_is_refused(self):
        assert "no objects" in self.rejects(SceneDocument())

    def test_a_plain_object_passes_through_untouched(self):
        plain = SceneObject.with_meta(CHILD_LOCAL, "Hand placed")

        result = resolve(single_object_prefab(), plain)

        assert result.expanded == 0
        assert result.document.objects[0] is plain
