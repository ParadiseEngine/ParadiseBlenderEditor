"""Tests for the engine-component router.

Pinned against ``Paradise.Export.Data.AuthoredComponentRouter``: an authored engine payload
must land in the same typed slot (or on the entity itself, for identity) that the C# router
would put it in, or the same scene authored in the two hosts stops meaning the same thing.
"""

from __future__ import annotations

from paradise_blender.contract import (
    authoring,
    authoring_router,
    component_ids,
    schema,
)


def entity() -> schema.LevelEntityData:
    return schema.LevelEntityData(id="Thing", kind="Prop", spawn_phase="LevelStart")


def engine_component(component_id: str) -> authoring.AuthoredComponentSchema:
    document = authoring.read_engine_schema()
    for component in document.components:
        if component.id == component_id:
            return component
    raise AssertionError(f"{component_id} not in the engine schema")


def payload(component_id: str, values: dict) -> dict:
    """The wire payload the exporter would build: schema defaults filled, values on top."""
    return authoring.build_payload(engine_component(component_id), values)


class TestVendoredEngineSchema:
    def test_the_engine_schema_loads_and_declares_the_known_components(self):
        ids = {c.id for c in authoring.read_engine_schema().components}
        assert ids >= component_ids.engine_ids()

    def test_every_named_constant_still_names_a_real_component(self):
        """component_ids.py is transcribed from ParadiseComponentIds.cs by hand and nothing keeps
        the two in step. This is the guard: a component the engine renames or drops fails here
        instead of leaving a constant that resolves to nothing at runtime.

        Deliberately NOT an equality check against the schema. Two reasons: the module is not a
        complete mirror -- a constant earns its place by being referenced -- so a component ADDED
        upstream must not fail this; and `engine_ids()` now derives from that same schema, so an
        equality assertion would compare the file with itself and could never fail at all."""
        ids = {c.id for c in authoring.read_engine_schema().components}
        named = {
            value for name, value in vars(component_ids).items()
            if name.isupper() and isinstance(value, str)
        }
        assert named, "no constants found -- the introspection above stopped matching"
        assert named <= ids

    def test_every_routed_id_exists_in_the_engine_schema(self):
        ids = {c.id for c in authoring.read_engine_schema().components}
        assert ids >= authoring_router.ROUTED_IDS


class TestIdentity:
    def test_spreads_across_the_entity_itself(self):
        target = entity()
        applied = authoring_router.apply(
            target, component_ids.IDENTITY,
            payload(component_ids.IDENTITY, {"Kind": "Car", "IsActive": False}))
        assert applied
        assert target.kind == "Car"
        assert target.is_active is False

    def test_blank_overridables_leave_the_exporter_values_alone(self):
        target = entity()
        target.prefab = "Models/Car.glb"
        target.display_name = None
        authoring_router.apply(
            target, component_ids.IDENTITY, payload(component_ids.IDENTITY, {}))
        assert target.prefab == "Models/Car.glb"
        assert target.spawn_phase == "LevelStart"
        assert target.kind == "Prop"


class TestNormalization:
    """What survived the typed slots.

    These used to assert that four components landed in four named slots, unpacked field by
    field. Nothing is unpacked on the way out any more — a payload rides verbatim — so what is
    left to pin is the clamping the contract does and NOTHING on the reading side calls: the
    ValidateAndNormalize methods exist in C# but no runtime path invokes them, which makes this
    the editor's job and this module the place it happens.
    """

    def test_audio_emitter_is_normalized_like_the_contract(self):
        emitted = authoring_router.normalize(
            component_ids.AUDIO_EMITTER,
            payload(component_ids.AUDIO_EMITTER, {"StartEvent": "Play_X", "AttenuationScale": 0.0}))
        assert emitted["StartEvent"] == "Play_X"
        assert emitted["AttenuationScale"] == 1.0  # zero scale repaired, as the contract does
        assert emitted["StopEvent"] is None  # blank became null, not ""

    def test_particles_map_color_and_leave_the_baked_sheet_absent(self):
        emitted = authoring_router.normalize(
            component_ids.PARTICLE_EMITTER,
            payload(component_ids.PARTICLE_EMITTER,
                    {"Kind": "Voxel", "Color": [0.2, 0.4, 0.6, 1.0], "MaxParticles": 16}))
        assert emitted["Kind"] == "Voxel"
        assert emitted["MaxParticles"] == 16
        assert emitted["Sheet"] is None

    def test_a_component_with_no_normalization_rides_verbatim(self):
        """Agent and rigidbody used to be unpacked into slots field by field. They are not
        touched now, and that is the point: the exporter stopped rewriting what an author typed."""
        original = payload(component_ids.RIGIDBODY, {"BodyType": "Dynamic", "Mass": 2.5})
        assert authoring_router.normalize(component_ids.RIGIDBODY, original) is original

    def test_a_game_component_is_never_touched(self):
        original = {"anything": [1, 2, 3]}
        assert authoring_router.normalize(
            "c4e8a1b2-9f60-4d33-8a17-6b2e50d9fc84", original) is original


class TestRefusals:
    def test_host_owned_and_unknown_engine_ids_are_not_routed(self):
        target = entity()
        for component_id in (component_ids.RENDERABLE, component_ids.COLLIDER, component_ids.LIGHT,
                             component_ids.SPRITE_ANIMATION, "7b1e0d4a-2c95-4f88-b3e6-05a9d1c7e264"):
            assert authoring_router.apply(target, component_id, {}) is False

    def test_game_ids_are_not_routed(self):
        assert authoring_router.apply(entity(), "c4e8a1b2-9f60-4d33-8a17-6b2e50d9fc84", {}) is False


class TestEngineIdsAreRecognizableWithoutAPrefix:
    """What the GUID switch broke about telling an engine component from a game one.

    The exporter used to ask ``component_id.startswith("paradise.")``. That question is now
    unanswerable -- an id carries no namespace at all -- so the only way to recognise one is
    membership in a known set. These pin the reason, since the branch that consumes it is
    defensive and nothing else would notice if the set went stale.
    """

    def test_no_engine_id_carries_the_old_prefix(self):
        assert not any(i.startswith("paradise.") for i in component_ids.engine_ids())

    def test_a_game_id_is_not_mistaken_for_an_engine_one(self):
        assert "c4e8a1b2-9f60-4d33-8a17-6b2e50d9fc84" not in component_ids.engine_ids()

    def test_every_routed_id_is_an_engine_id(self):
        assert component_ids.engine_ids() >= authoring_router.ROUTED_IDS
