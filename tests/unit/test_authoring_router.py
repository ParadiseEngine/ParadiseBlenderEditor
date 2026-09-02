"""Tests for the engine-component router.

What is left of it. Routing was a dispatch that decided WHERE a payload landed — nine typed slots
once, then one exception for identity, which was spread onto the entity record because it was what
the entity WAS. Schema v5 deleted the record, and with it the exception; every payload rides
verbatim into the object's component list.

So the subject here is the half that survived: the normalization the contract declares and NOTHING
on the reading side calls. ``ValidateAndNormalize`` exists in C# and no runtime path invokes it,
which makes clamping the editor's job — and this the module that does it.
"""

from __future__ import annotations

from paradise_blender.contract import (
    authoring,
    authoring_router,
    component_ids,
)


def _field(name: str, type_: str, default=None, has_default: bool = True):
    return authoring.AuthoredFieldSchema(
        name=name, type=type_, default=default, has_default=has_default)


# The engine component shapes these tests build payloads from, declared HERE rather than read out
# of a document.
#
# They used to come from a vendored copy of the engine's schema, which this host no longer keeps:
# the game's dumped document describes the engine's components now (the launcher that dumps it
# scans its references), and that document belongs to a game, not to a test. Only the fields these
# tests actually exercise are declared — the subject is the ROUTER, which decides where a payload
# lands and what normalization clamps, and neither answer depends on the fields it does not read.
_SHAPES: dict[str, authoring.AuthoredComponentSchema] = {
    component_ids.AUDIO_EMITTER: authoring.AuthoredComponentSchema(
        id=component_ids.AUDIO_EMITTER,
        type="Paradise.Export.Data.AudioEmitterComponentData",
        display_name="Audio emitter",
        fields=[
            _field("StartEvent", authoring.TYPE_STRING, ""),
            _field("StopEvent", authoring.TYPE_STRING, ""),
            _field("PlayOnStart", authoring.TYPE_BOOL, True),
            _field("Is3D", authoring.TYPE_BOOL, True),
            _field("AttenuationScale", authoring.TYPE_FLOAT, 1.0),
        ],
    ),
    component_ids.PARTICLE_EMITTER: authoring.AuthoredComponentSchema(
        id=component_ids.PARTICLE_EMITTER,
        type="Paradise.Export.Data.ParticleEmitterComponentData",
        display_name="Particle emitter",
        fields=[
            _field("Kind", authoring.TYPE_STRING, ""),
            _field("Color", authoring.TYPE_COLOR, [1.0, 1.0, 1.0, 1.0]),
            _field("MaxParticles", authoring.TYPE_INT, 0),
            _field("EmitRate", authoring.TYPE_FLOAT, 1.0),
            _field("LifetimeSeconds", authoring.TYPE_FLOAT, 1.0),
            _field("InitialSpeed", authoring.TYPE_FLOAT, 0.0),
            _field("SpreadDegrees", authoring.TYPE_FLOAT, 0.0),
            _field("Gravity", authoring.TYPE_FLOAT, 0.0),
            _field("Drag", authoring.TYPE_FLOAT, 0.0),
            _field("StartSize", authoring.TYPE_FLOAT, 1.0),
            _field("EndSize", authoring.TYPE_FLOAT, 1.0),
            _field("Seed", authoring.TYPE_INT, 0),
            _field("Columns", authoring.TYPE_INT, 1),
            _field("Rows", authoring.TYPE_INT, 1),
            _field("FrameCount", authoring.TYPE_INT, 1),
            _field("Fps", authoring.TYPE_FLOAT, 1.0),
        ],
    ),
    component_ids.RIGIDBODY: authoring.AuthoredComponentSchema(
        id=component_ids.RIGIDBODY,
        type="Paradise.Export.Data.RigidbodyComponentData",
        display_name="Rigidbody",
        fields=[
            _field("BodyType", authoring.TYPE_STRING, "Static"),
            _field("Mass", authoring.TYPE_FLOAT, 0.0),
        ],
    ),
}


def engine_component(component_id: str) -> authoring.AuthoredComponentSchema:
    return _SHAPES[component_id]


def payload(component_id: str, values: dict) -> dict:
    """The wire payload the exporter would build: schema defaults filled, values on top."""
    return authoring.build_payload(engine_component(component_id), values)


class TestNormalization:

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
    def test_a_component_with_no_normalization_is_returned_unchanged(self):
        """Every id but the two clamped ones, including host-owned and unknown engine components
        and a game's own. There is no longer any id the router treats SPECIALLY beyond clamping,
        which is what makes this a one-liner rather than a table of destinations."""
        for component_id in (component_ids.RENDERABLE, component_ids.COLLIDER, component_ids.LIGHT,
                             component_ids.SPRITE_ANIMATION, component_ids.ENVIRONMENT,
                             "7b1e0d4a-2c95-4f88-b3e6-05a9d1c7e264"):
            original = {"anything": [1, 2, 3]}
            assert authoring_router.normalize(component_id, original) is original

    def test_only_the_two_clamped_ids_are_routed(self):
        assert set(authoring_router.ROUTED_IDS) == {
            component_ids.AUDIO_EMITTER, component_ids.PARTICLE_EMITTER}
