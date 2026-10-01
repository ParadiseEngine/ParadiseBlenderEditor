"""Selection composition preserves geometry, reference addresses, and input documents."""

from __future__ import annotations

import copy

import pytest

from paradise_assets.document import asset_reference, new_prefab, overrides, prefab, well_known
from paradise_assets.document.asset_reference import AssetReference
from paradise_assets.document.new_prefab import CreateError
from paradise_assets.document.prefab import PrefabComponent, PrefabObject
from paradise_assets.materialize.selection_prefab import SelectionPlan, compose

SELECTION_ROOT = "aaaaaaaa-0000-4000-8000-000000000001"
REFERENCE = "aaaaaaaa-0000-4000-8000-000000000002"
RAW_ROOT = "bbbbbbbb-0000-4000-8000-000000000001"
RAW_INSTANCE = "bbbbbbbb-0000-4000-8000-000000000002"
RAW_LEAF = "bbbbbbbb-0000-4000-8000-000000000003"
CARRIER = "bbbbbbbb-0000-4000-8000-000000000004"
MESH_COMPONENT = "cccccccc-0000-4000-8000-000000000001"

# Matching object IDs must not cause asset identities to be remapped.
PREFAB_ASSET = AssetReference(RAW_ROOT, "prefabs/Original.prefab")
MESH_ASSET = AssetReference(RAW_INSTANCE, "meshes/Raw.mesh")
MATERIAL_ASSET = AssetReference(RAW_LEAF, "materials/Raw.material")


def mesh_component():
    return PrefabComponent(MESH_COMPONENT, "Game.StaticMesh", {
        "Mesh": asset_reference.write(MESH_ASSET),
        "Materials": [asset_reference.write(MATERIAL_ASSET)],
        "Settings": {"Weights": [0.25, 0.75]},
    })


@pytest.fixture
def reference_document():
    document = new_prefab.root_only("Selection", SELECTION_ROOT)
    instance = PrefabObject.with_meta(REFERENCE, "Existing reference", SELECTION_ROOT)
    instance.prefab = PREFAB_ASSET
    instance.components.append(mesh_component())
    document.objects.append(instance)
    return document


@pytest.fixture
def raw_seed():
    document = new_prefab.root_only("Raw geometry", RAW_ROOT)
    document.root().component(well_known.TRANSFORM_ID).data = {
        well_known.POSITION: [4.0, -2.0, 7.0],
        well_known.ROTATION: [0.0, 0.0, 1.0, 0.0],
        well_known.SCALE: [2.0, 3.0, 4.0],
    }
    document.root().components.append(mesh_component())
    instance = PrefabObject.with_meta(RAW_INSTANCE, "Nested instance", RAW_ROOT)
    instance.prefab = PREFAB_ASSET
    leaf = PrefabObject.with_meta(RAW_LEAF, "Nested geometry", RAW_INSTANCE)
    leaf.components.append(mesh_component())

    # Targets are prefab-local addresses, even when they spell a seed object's ID.
    carrier = overrides.new_carrier(RAW_INSTANCE, RAW_ROOT)
    identified_carrier = overrides.new_carrier(RAW_INSTANCE, RAW_LEAF)
    identified_carrier.meta.data[well_known.GUID] = CARRIER
    document.objects.extend([instance, leaf, carrier, identified_carrier])
    return document


@pytest.fixture
def mixed_plan(reference_document):
    return SelectionPlan(
        raw=[object()], document=reference_document, origin=(10.0, 20.0, 30.0), has_references=True,
    )


def test_raw_only_returns_the_exact_seed(reference_document, raw_seed):
    plan = SelectionPlan(
        raw=[object()], document=reference_document, origin=None, has_references=False,
    )
    document_before = copy.deepcopy(plan.document)
    seed_before = copy.deepcopy(raw_seed)

    result = compose(plan, raw_seed)

    assert result is raw_seed
    assert result == seed_before
    assert plan.document == document_before


def test_raw_only_refuses_a_missing_seed(reference_document):
    plan = SelectionPlan(
        raw=[object()], document=reference_document, origin=None, has_references=False,
    )
    before = copy.deepcopy(plan.document)

    with pytest.raises(CreateError, match="Raw geometry requires an extracted prefab seed"):
        compose(plan, None)

    assert plan.document == before


def test_reference_only_keeps_the_identity_root_and_reference(reference_document):
    plan = SelectionPlan(raw=[], document=reference_document, origin=(10.0, 20.0, 30.0), has_references=True)

    result = compose(plan, None)

    assert result == reference_document
    assert result.root_guid == SELECTION_ROOT
    assert result.root().parent is None
    assert result.root().component(well_known.TRANSFORM_ID).data == new_prefab.IDENTITY_TRANSFORM
    assert result.by_guid()[REFERENCE].parent == SELECTION_ROOT
    assert result.by_guid()[REFERENCE].prefab == PREFAB_ASSET
    assert prefab.loads(prefab.dumps(result)) == result


@pytest.mark.parametrize("include_raw", [False, True])
def test_composition_keeps_carriers_after_their_owner(reference_document, raw_seed, include_raw):
    carrier = overrides.new_carrier(REFERENCE, RAW_LEAF)
    carrier.components.append(mesh_component())
    following = PrefabObject.with_meta(RAW_INSTANCE, "Following reference", SELECTION_ROOT)
    following.prefab = PREFAB_ASSET
    reference_document.objects.extend([carrier, following])
    plan = SelectionPlan(
        raw=[object()] if include_raw else [],
        document=reference_document,
        origin=None,
        has_references=True,
    )
    before = copy.deepcopy(reference_document)

    result = compose(plan, raw_seed if include_raw else None)

    assert result.objects[:4] == before.objects
    assert result.objects[2].target == RAW_LEAF
    assert result.objects[2].parent == result.objects[1].guid
    assert result.objects[3].guid == following.guid
    assert prefab.loads(prefab.dumps(result)).objects[:4] == before.objects
    assert reference_document == before


def test_reference_only_returns_a_deeply_independent_document(reference_document):
    plan = SelectionPlan(raw=[], document=reference_document, origin=None, has_references=True)
    before = copy.deepcopy(reference_document)

    result = compose(plan, None)

    assert result == before
    assert reference_document == before
    assert result is not reference_document
    assert result.objects is not reference_document.objects
    for original, cloned in zip(reference_document.objects, result.objects, strict=True):
        assert cloned is not original
        assert cloned.components is not original.components
        for original_component, cloned_component in zip(original.components, cloned.components, strict=True):
            assert cloned_component is not original_component
            assert cloned_component.data is not original_component.data

    result.root().component(well_known.TRANSFORM_ID).data[well_known.POSITION][0] = 99.0
    result.by_guid()[REFERENCE].component(MESH_COMPONENT).data["Settings"]["Weights"].append(1.0)
    result.objects.pop()
    assert reference_document == before


def test_mixed_keeps_the_raw_root_transform_and_all_geometry(mixed_plan, raw_seed):
    result = compose(mixed_plan, raw_seed)
    offset = len(mixed_plan.document.objects)
    appended = result.objects[offset:]

    assert result.root_guid == SELECTION_ROOT
    assert result.root().component(well_known.TRANSFORM_ID).data == new_prefab.IDENTITY_TRANSFORM
    assert result.objects[:offset] == mixed_plan.document.objects
    assert len(appended) == len(raw_seed.objects)
    assert appended[0].parent == SELECTION_ROOT
    assert appended[0].name == raw_seed.root().name
    assert appended[0].component(well_known.TRANSFORM_ID) == raw_seed.root().component(
        well_known.TRANSFORM_ID
    )
    assert appended[0].component(MESH_COMPONENT) == raw_seed.root().component(MESH_COMPONENT)
    assert appended[2].component(MESH_COMPONENT) == raw_seed.by_guid()[RAW_LEAF].component(MESH_COMPONENT)
    for original, cloned in zip(raw_seed.objects, appended, strict=True):
        assert [c for c in cloned.components if c.id != well_known.META_ID] == [
            c for c in original.components if c.id != well_known.META_ID
        ]
    assert prefab.loads(prefab.dumps(result)) == result


def test_mixed_mints_fresh_ids_and_remaps_parents_but_not_carrier_targets(mixed_plan, raw_seed):
    result = compose(mixed_plan, raw_seed)
    offset = len(mixed_plan.document.objects)
    appended = result.objects[offset:]
    mapping = {
        original.guid: cloned.guid
        for original, cloned in zip(raw_seed.objects, appended, strict=True)
        if original.guid is not None
    }
    original_ids = set(raw_seed.by_guid()) | set(mixed_plan.document.by_guid())
    result_ids = [entry.guid for entry in result.objects if entry.guid is not None]

    assert set(mapping) == {RAW_ROOT, RAW_INSTANCE, RAW_LEAF, CARRIER}
    assert None not in mapping.values()
    assert set(mapping.values()).isdisjoint(original_ids)
    assert len(result_ids) == len(set(result_ids))
    for original, cloned in zip(raw_seed.objects, appended, strict=True):
        expected_parent = SELECTION_ROOT if original is raw_seed.root() else mapping[original.parent]
        assert cloned.parent == expected_parent
        assert cloned.target == original.target
        assert cloned.name == original.name
        if original.guid is None:
            assert cloned.guid is None

    carriers = [entry for entry in appended if entry.target is not None]
    assert len(carriers) == 2
    assert {entry.target for entry in carriers} == {RAW_ROOT, RAW_LEAF}
    assert all(entry.parent == mapping[RAW_INSTANCE] for entry in carriers)
    second = compose(mixed_plan, raw_seed)
    second_ids = {entry.guid for entry in second.objects[offset:] if entry.guid is not None}
    assert set(mapping.values()).isdisjoint(second_ids)


def test_mixed_preserves_asset_guids_and_paths(mixed_plan, raw_seed):
    result = compose(mixed_plan, raw_seed)
    sources = mixed_plan.document.objects + raw_seed.objects

    for original, cloned in zip(sources, result.objects, strict=True):
        assert cloned.prefab == original.prefab
        if original.prefab is not None:
            assert (cloned.prefab.guid, cloned.prefab.path) == (PREFAB_ASSET.guid, PREFAB_ASSET.path)
        if original.component(MESH_COMPONENT) is not None:
            data = cloned.component(MESH_COMPONENT).data
            assert data["Mesh"] == {"guid": MESH_ASSET.guid, "path": MESH_ASSET.path}
            assert data["Materials"] == [{"guid": MATERIAL_ASSET.guid, "path": MATERIAL_ASSET.path}]


def test_mixed_does_not_mutate_or_share_nested_payloads_with_either_input(mixed_plan, raw_seed):
    document_before = copy.deepcopy(mixed_plan.document)
    seed_before = copy.deepcopy(raw_seed)

    result = compose(mixed_plan, raw_seed)

    assert mixed_plan.document == document_before
    assert raw_seed == seed_before
    sources = mixed_plan.document.objects + raw_seed.objects
    for original, cloned in zip(sources, result.objects, strict=True):
        assert cloned is not original
        assert cloned.components is not original.components
        for original_component, cloned_component in zip(original.components, cloned.components, strict=True):
            assert cloned_component is not original_component
            assert cloned_component.data is not original_component.data
        cloned.meta.data[well_known.NAME] = "Changed in composed document"
        mesh = cloned.component(MESH_COMPONENT)
        if mesh is not None:
            mesh.data["Materials"][0]["path"] = "materials/Changed.material"
            mesh.data["Settings"]["Weights"].append(1.0)
    result.objects[len(mixed_plan.document.objects)].component(well_known.TRANSFORM_ID).data[
        well_known.POSITION
    ][0] = 99.0
    result.objects.clear()
    assert mixed_plan.document == document_before
    assert raw_seed == seed_before


def test_mixed_refuses_a_missing_seed(mixed_plan):
    before = copy.deepcopy(mixed_plan.document)

    message = "mixed selection requires an extracted prefab seed for its raw meshes"
    with pytest.raises(CreateError, match=message):
        compose(mixed_plan, None)

    assert mixed_plan.document == before
