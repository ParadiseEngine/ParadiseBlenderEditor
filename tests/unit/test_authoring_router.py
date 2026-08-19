"""Tests for the engine-component router.

Pinned against ``Paradise.Export.Data.AuthoredComponentRouter``: an authored engine payload
must land in the same typed slot (or on the entity itself, for identity) that the C# router
would put it in, or the same scene authored in the two hosts stops meaning the same thing.
"""

from __future__ import annotations

import json

from paradise_blender.contract import authoring, authoring_router, schema


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
        assert {
            "paradise.identity",
            "paradise.agent",
            "paradise.rigidbody",
            "paradise.audio-emitter",
            "paradise.particle-emitter",
            "paradise.renderable",
            "paradise.collider",
            "paradise.interactable",
            "paradise.light",
            "paradise.sprite-animation",
        } <= ids

    def test_every_routed_id_exists_in_the_engine_schema(self):
        ids = {c.id for c in authoring.read_engine_schema().components}
        assert ids >= authoring_router.ROUTED_IDS


class TestIdentity:
    def test_spreads_across_the_entity_itself(self):
        target = entity()
        applied = authoring_router.apply(
            target, "paradise.identity",
            payload("paradise.identity", {"Kind": "Car", "IsActive": False}))
        assert applied
        assert target.kind == "Car"
        assert target.is_active is False

    def test_blank_overridables_leave_the_exporter_values_alone(self):
        target = entity()
        target.prefab = "Models/Car.glb"
        target.display_name = None
        authoring_router.apply(
            target, "paradise.identity", payload("paradise.identity", {}))
        assert target.prefab == "Models/Car.glb"
        assert target.spawn_phase == "LevelStart"
        assert target.kind == "Prop"


class TestTypedSlots:
    def test_agent_lands_in_its_slot_with_blank_clips_as_null(self):
        target = entity()
        authoring_router.apply(
            target, "paradise.agent",
            payload("paradise.agent", {"MoveSpeed": 4.0, "IdleClip": "Idle_Loop", "WalkClip": ""}))
        agent = target.components.agent
        assert agent is not None
        assert agent.move_speed == 4.0
        assert agent.idle_clip == "Idle_Loop"
        assert agent.walk_clip is None

    def test_rigidbody_travels_verbatim_with_the_enum_by_name(self):
        target = entity()
        authoring_router.apply(
            target, "paradise.rigidbody",
            payload("paradise.rigidbody", {"BodyType": "Dynamic", "Mass": 2.5}))
        body = target.components.rigidbody
        assert body is not None
        assert body.to_json()["BodyType"] == "Dynamic"
        assert body.mass == 2.5

    def test_audio_emitter_is_normalized_like_the_contract(self):
        target = entity()
        authoring_router.apply(
            target, "paradise.audio-emitter",
            payload("paradise.audio-emitter", {"StartEvent": "Play_X", "AttenuationScale": 0.0}))
        audio = target.components.audio_emitter
        assert audio is not None
        assert audio.start_event == "Play_X"
        assert audio.attenuation_scale == 1.0  # zero scale repaired, as ValidateAndNormalize does

    def test_particles_map_color_and_leave_the_baked_sheet_absent(self):
        target = entity()
        authoring_router.apply(
            target, "paradise.particle-emitter",
            payload("paradise.particle-emitter",
                    {"Kind": "Voxel", "Color": [0.2, 0.4, 0.6, 1.0], "MaxParticles": 16}))
        particles = target.components.particle_emitter
        assert particles is not None
        emitted = particles.to_json()
        assert emitted["Kind"] == "Voxel"
        assert emitted["MaxParticles"] == 16
        assert emitted["Sheet"] is None

    def test_the_routed_payloads_survive_the_document_writer(self):
        target = entity()
        for component_id in ("paradise.agent", "paradise.rigidbody"):
            authoring_router.apply(target, component_id, payload(component_id, {}))
        text = json.dumps(target.to_json())
        assert '"Agent"' in text and '"Rigidbody"' in text


class TestRefusals:
    def test_host_owned_and_unknown_engine_ids_are_not_routed(self):
        target = entity()
        for component_id in ("paradise.renderable", "paradise.collider", "paradise.light",
                             "paradise.sprite-animation", "paradise.does-not-exist"):
            assert authoring_router.apply(target, component_id, {}) is False

    def test_game_ids_are_not_routed(self):
        assert authoring_router.apply(entity(), "game.creature", {}) is False
