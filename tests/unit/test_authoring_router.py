"""Tests for the engine-component router.

Pinned against ``Paradise.Export.Data.AuthoredComponentRouter``: an authored engine payload
must land in the same typed slot (or on the entity itself, for identity) that the C# router
would put it in, or the same scene authored in the two hosts stops meaning the same thing.
"""

from __future__ import annotations

import json

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
        assert ids >= component_ids.ENGINE_IDS

    def test_the_vendored_ids_still_match_the_vendored_schema(self):
        """component_ids.py is transcribed from ParadiseComponentIds.cs by hand and nothing keeps
        the two in step. This is the guard: regenerate the schema and a constant that no longer
        names a real component fails here instead of resolving to nothing at runtime."""
        ids = {c.id for c in authoring.read_engine_schema().components}
        assert ids == component_ids.ENGINE_IDS

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


class TestTypedSlots:
    def test_agent_lands_in_its_slot_with_blank_clips_as_null(self):
        target = entity()
        authoring_router.apply(
            target, component_ids.AGENT,
            payload(component_ids.AGENT, {"MoveSpeed": 4.0, "IdleClip": "Idle_Loop", "WalkClip": ""}))
        agent = target.components.agent
        assert agent is not None
        assert agent.move_speed == 4.0
        assert agent.idle_clip == "Idle_Loop"
        assert agent.walk_clip is None

    def test_rigidbody_travels_verbatim_with_the_enum_by_name(self):
        target = entity()
        authoring_router.apply(
            target, component_ids.RIGIDBODY,
            payload(component_ids.RIGIDBODY, {"BodyType": "Dynamic", "Mass": 2.5}))
        body = target.components.rigidbody
        assert body is not None
        assert body.to_json()["BodyType"] == "Dynamic"
        assert body.mass == 2.5

    def test_audio_emitter_is_normalized_like_the_contract(self):
        target = entity()
        authoring_router.apply(
            target, component_ids.AUDIO_EMITTER,
            payload(component_ids.AUDIO_EMITTER, {"StartEvent": "Play_X", "AttenuationScale": 0.0}))
        audio = target.components.audio_emitter
        assert audio is not None
        assert audio.start_event == "Play_X"
        assert audio.attenuation_scale == 1.0  # zero scale repaired, as ValidateAndNormalize does

    def test_particles_map_color_and_leave_the_baked_sheet_absent(self):
        target = entity()
        authoring_router.apply(
            target, component_ids.PARTICLE_EMITTER,
            payload(component_ids.PARTICLE_EMITTER,
                    {"Kind": "Voxel", "Color": [0.2, 0.4, 0.6, 1.0], "MaxParticles": 16}))
        particles = target.components.particle_emitter
        assert particles is not None
        emitted = particles.to_json()
        assert emitted["Kind"] == "Voxel"
        assert emitted["MaxParticles"] == 16
        assert emitted["Sheet"] is None

    def test_the_routed_payloads_survive_the_document_writer(self):
        target = entity()
        for component_id in (component_ids.AGENT, component_ids.RIGIDBODY):
            authoring_router.apply(target, component_id, payload(component_id, {}))
        text = json.dumps(target.to_json())
        assert '"Agent"' in text and '"Rigidbody"' in text


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
        assert not any(i.startswith("paradise.") for i in component_ids.ENGINE_IDS)

    def test_a_game_id_is_not_mistaken_for_an_engine_one(self):
        assert "c4e8a1b2-9f60-4d33-8a17-6b2e50d9fc84" not in component_ids.ENGINE_IDS

    def test_every_routed_id_is_an_engine_id(self):
        assert authoring_router.ROUTED_IDS <= component_ids.ENGINE_IDS
