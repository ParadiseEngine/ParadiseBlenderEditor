"""Entity references (``authoredBy: entity``): what they may name, and what the save refuses.

The promises under test (#70, #71):

- a reference may name a document entry, an instance root, a resolved child of an instance and
  content of a prefab nested inside that prefab -- every guid the RESOLVED document contains;
- the values a document spells itself are errors when they name nothing; values a placed prefab
  contributes are warnings, since this document cannot fix them;
- an unset field (absent, ``""``, the zero guid) is not a dangling name;
- a duplicated instance is a different entity: the reference keeps naming the original's child.
"""

from __future__ import annotations

from paradise_assets.document import component_schema, entity_refs, overrides, resolve, well_known
from paradise_assets.document.asset_reference import AssetReference
from paradise_assets.document.prefab import PrefabComponent, PrefabDocument, PrefabObject

LEVEL_ROOT = "10000000-0000-4000-8000-000000000001"
CAMERA = "10000000-0000-4000-8000-000000000002"
SHOP = "10000000-0000-4000-8000-000000000003"
SHOP_COPY = "10000000-0000-4000-8000-000000000004"

SHOP_ROOT = "20000000-0000-4000-8000-000000000001"
DOOR = "20000000-0000-4000-8000-000000000002"
SIGN_INSTANCE = "20000000-0000-4000-8000-000000000003"

SIGN_ROOT = "30000000-0000-4000-8000-000000000001"
BULB = "30000000-0000-4000-8000-000000000002"

TARGET_ID = "9519caf9-7a87-4f72-845d-d59695cac8b9"
ROUTE_ID = "aaaaaaaa-0000-4000-8000-0000000000aa"

SHOP_REF = AssetReference("50000000-0000-4000-8000-000000000001", "prefabs/shop.prefab")
SIGN_REF = AssetReference("50000000-0000-4000-8000-000000000002", "prefabs/sign.prefab")


def _vocabulary() -> component_schema.Vocabulary:
    target = component_schema.ComponentSchema({
        "id": TARGET_ID, "displayName": "Camera target",
        "fields": [{"name": "Object", "type": "string", "authoredBy": "entity"}],
    })
    route = component_schema.ComponentSchema({
        "id": ROUTE_ID, "displayName": "Route",
        "fields": [
            {"name": "Label", "type": "string"},
            {"name": "Stops", "type": "array",
             "items": {"name": "Stops", "type": "string", "authoredBy": "entity"}},
            {"name": "Legs", "type": "array", "items": {"name": "Legs", "type": "object", "fields": [
                {"name": "To", "type": "string", "authoredBy": "entity"},
                {"name": "Seconds", "type": "float"},
            ]}},
        ],
    })
    return component_schema.Vocabulary({TARGET_ID: target, ROUTE_ID: route}, None)


def _target(value) -> PrefabComponent:
    return PrefabComponent(TARGET_ID, "ShiningPie.Authoring.CameraTarget", {"Object": value})


def _obj(guid, name, parent=None, *components, prefab=None) -> PrefabObject:
    entry = PrefabObject.with_meta(guid, name, parent)
    entry.prefab = prefab
    entry.components.extend(components)
    return entry


def _prefabs():
    sign = PrefabDocument([_obj(SIGN_ROOT, "Sign"), _obj(BULB, "Bulb", SIGN_ROOT)])
    shop = PrefabDocument([
        _obj(SHOP_ROOT, "Shop"),
        # A prefab naming its OWN member by its local guid: valid in shop.prefab, dangling in
        # every instance of it.
        _obj(DOOR, "Door", SHOP_ROOT, _target(SIGN_INSTANCE)),
        _obj(SIGN_INSTANCE, "Sign", SHOP_ROOT, prefab=SIGN_REF),
    ])
    files = {SHOP_REF.path: shop, SIGN_REF.path: sign}
    return lambda reference: files.get(reference.path)


def _level(*camera_components, extra=()) -> PrefabDocument:
    return PrefabDocument([
        _obj(LEVEL_ROOT, "Level"),
        _obj(CAMERA, "Camera", LEVEL_ROOT, *camera_components),
        _obj(SHOP, "Shop", LEVEL_ROOT, prefab=SHOP_REF),
        *extra,
    ])


def _check(document: PrefabDocument) -> entity_refs.Report:
    prefabs = _prefabs()
    resolved = resolve.resolve(document, prefabs)
    assert resolved.errors == []
    return entity_refs.check(
        document, resolved.document, _vocabulary(), overrides.locals_of(document, prefabs))


def _messages(problems) -> list[str]:
    return [problem.message() for problem in problems]


def test_a_reference_to_an_entry_an_instance_root_a_child_and_a_nested_child_is_valid():
    door = resolve.mint_child_guid(SHOP, DOOR)
    sign = resolve.mint_child_guid(SHOP, SIGN_INSTANCE)
    # The bulb lives in sign.prefab, nested in shop.prefab: flattening mints its shop-local
    # guid first, and the level mints over that -- the address a carrier spells too.
    bulb = resolve.mint_child_guid(SHOP, resolve.mint_child_guid(SIGN_INSTANCE, BULB))
    for value in (LEVEL_ROOT, SHOP, door, sign, bulb, bulb.upper()):
        report = _check(_level(_target(value)))
        assert report.errors == [], value


def test_targets_name_children_by_where_they_live():
    document = _level()
    prefabs = _prefabs()
    found = entity_refs.targets(
        resolve.resolve(document, prefabs).document, overrides.locals_of(document, prefabs))
    door = found[resolve.mint_child_guid(SHOP, DOOR)]
    assert door.label == "Door  (Shop)"
    assert (door.instance, door.local) == (SHOP, DOOR)
    assert found[SHOP].instance is None
    bulb = found[resolve.mint_child_guid(SHOP, resolve.mint_child_guid(SIGN_INSTANCE, BULB))]
    assert bulb.label == "Bulb  (Sign / Shop)"


def test_unset_values_are_not_dangling():
    for value in ("", "00000000-0000-0000-0000-000000000000"):
        assert _check(_level(_target(value))).errors == []
    absent = PrefabComponent(TARGET_ID, None, {})
    assert _check(_level(absent)).errors == []


def test_a_prefab_local_guid_names_nothing_in_the_level():
    report = _check(_level(_target(DOOR)))
    assert _messages(report.errors) == [
        f"Camera > Camera target.Object names {DOOR}, which is no object in this document"
    ]


def test_a_name_is_not_an_identity():
    report = _check(_level(_target("Player")))
    assert len(report.errors) == 1
    assert "'Player', which is not an object identity" in report.errors[0].reason


def test_a_deleted_instance_or_removed_member_is_an_error_not_a_silent_guid():
    door = resolve.mint_child_guid(SHOP, DOOR)
    gone = PrefabDocument([_obj(LEVEL_ROOT, "Level"), _obj(CAMERA, "Camera", LEVEL_ROOT, _target(door))])
    prefabs = _prefabs()
    resolved = resolve.resolve(gone, prefabs).document
    report = entity_refs.check(gone, resolved, _vocabulary(), {})
    assert [p.value for p in report.errors] == [door]


def test_a_duplicated_instance_is_a_different_entity_and_the_reference_keeps_the_original():
    door = resolve.mint_child_guid(SHOP, DOOR)
    copy_door = resolve.mint_child_guid(SHOP_COPY, DOOR)
    assert door != copy_door
    document = _level(_target(door), extra=[_obj(SHOP_COPY, "Shop.001", LEVEL_ROOT, prefab=SHOP_REF)])
    assert _check(document).errors == []
    # Deleting the ORIGINAL leaves the copy's door, which the reference never aliased.
    document.objects = [entry for entry in document.objects if entry.guid != SHOP]
    assert [p.value for p in _check(document).errors] == [door]


def test_lists_and_nested_records_are_walked():
    route = PrefabComponent(ROUTE_ID, None, {
        "Label": "not-a-guid-and-not-an-entity",
        "Stops": [SHOP, "", DOOR],
        "Legs": [{"To": CAMERA, "Seconds": 1.0}, {"To": SIGN_ROOT, "Seconds": 2.0}],
    })
    report = _check(_level(route))
    assert [(p.path, p.value) for p in report.errors] == [
        ("Stops/2", DOOR), ("Legs/1/To", SIGN_ROOT)]
    assert report.errors[0].message().startswith("Camera > Route.Stops.2 names ")
    assert report.errors[1].message().startswith("Camera > Route.Legs.1.To names ")


def test_an_override_carrier_value_is_this_documents_own():
    carrier = overrides.new_carrier(SHOP, DOOR)
    carrier.components.append(_target(SIGN_ROOT))
    report = _check(_level(extra=[carrier]))
    assert len(report.errors) == 1
    assert report.errors[0].owner == resolve.mint_child_guid(SHOP, DOOR)
    assert report.errors[0].owner_name == "Door"
    # The override replaces the prefab's own dangling value, so no warning repeats it.
    assert report.warnings == []


def test_a_value_the_prefab_contributes_is_a_warning_not_a_refusal():
    report = _check(_level())
    assert report.errors == []
    assert len(report.warnings) == 1
    warning = report.warnings[0]
    assert warning.inherited and warning.value == SIGN_INSTANCE
    assert warning.message().endswith("(it comes from the placed prefab, so fix it there)")


def test_without_a_schema_nothing_is_known_to_be_a_reference():
    document = _level(_target("Player"))
    prefabs = _prefabs()
    resolved = resolve.resolve(document, prefabs).document
    empty = component_schema.Vocabulary({}, None)
    report = entity_refs.check(document, resolved, empty, {})
    assert report.errors == [] and report.warnings == []


def test_a_removed_component_carries_nothing_to_check():
    carrier = overrides.new_carrier(SHOP, DOOR)
    carrier.components.append(PrefabComponent(TARGET_ID, None, {}, removed=True))
    report = _check(_level(extra=[carrier]))
    # Removing the door's component takes the prefab's dangling value with it, too.
    assert report.errors == [] and report.warnings == []


def test_is_entity_field():
    vocabulary = _vocabulary()
    assert entity_refs.is_entity_field(vocabulary.get(TARGET_ID).field("Object"))
    route = vocabulary.get(ROUTE_ID)
    assert not entity_refs.is_entity_field(route.field("Stops"))
    assert entity_refs.is_entity_field(route.field("Stops").items)
    assert not entity_refs.is_entity_field(route.field("Label"))


def test_meta_and_transform_are_never_walked():
    entry = _obj(CAMERA, "Camera", LEVEL_ROOT)
    assert {c.id for c in entry.components} == {well_known.META_ID}
