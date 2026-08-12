"""Tests for the document schema mirror.

These assert *shape*, not values: key names, key order, which keys exist, and that enums
serialize by name. A shape mismatch is what breaks the engine's reader; a value mismatch is a
scene-authoring question.
"""

from __future__ import annotations

import json
import uuid

from paradise_blender.contract import schema, writer


class TestLevelData:
    def test_key_order_matches_the_csharp_declaration_order(self):
        assert list(schema.LevelData().to_json()) == [
            "SchemaVersion",
            "Camera",
            "Lighting",
            "NavMeshAgent",
            "Interactables",
            "Entities",
            "NavMeshFile",
            "Materials",
        ]

    def test_empty_document_matches_the_engine_golden_tail(self):
        """The engine's SampleScene fixture ends with these exact lines."""
        text = writer.dumps(schema.LevelData(schema_version=2).to_json())
        assert '"Interactables": [],' in text
        assert '"Entities": [],' in text
        assert '"NavMeshFile": null,' in text
        assert text.endswith('"Materials": []\n}')

    def test_schema_version_defaults_to_the_pinned_version(self):
        assert schema.LevelData().schema_version == schema.SCHEMA_VERSION == 2

    def test_ensure_lighting_state_creates_one_default_state(self):
        document = schema.LevelData()
        state = document.ensure_lighting_state()
        assert document.lighting.active_state == "Default"
        assert state.name == "Default"
        # Idempotent: a second call must reuse the same state, not append another.
        assert document.ensure_lighting_state() is state
        assert len(document.lighting.states) == 1


class TestLevelEntityData:
    def test_key_order(self):
        assert list(schema.LevelEntityData().to_json()) == [
            "Id",
            "EntityGuid",
            "StableId",
            "DisplayName",
            "Kind",
            "SpawnPhase",
            "IsActive",
            "Prefab",
            "PrefabAssetPath",
            "NearestInstanceRoot",
            "PrefabGuid",
            "PrefabAssetType",
            "InitialAnimation",
            "Parent",
            "LocalPosition",
            "LocalRotation",
            "LocalScale",
            "LocalMatrix",
            "WorldMatrix",
            "Materials",
            "Overrides",
            "Components",
        ]

    def test_guid_uses_the_hyphenated_lowercase_form(self):
        """System.Text.Json's default Guid format ("D"). The engine parses this on read."""
        value = uuid.UUID("e63c73bc-a31b-48a7-b741-1a9eb265ff98")
        assert schema.LevelEntityData(entity_guid=value).to_json()["EntityGuid"] == (
            "e63c73bc-a31b-48a7-b741-1a9eb265ff98"
        )

    def test_overrides_are_present_but_empty(self):
        """Neither authoring host can populate per-property overrides, but the key set must
        stay stable or the reader sees a shape change."""
        assert schema.LevelEntityData().to_json()["Overrides"] == {
            "Transform": False,
            "MaterialSlots": [],
            "Colliders": [],
            "Metadata": [],
        }

    def test_absent_components_serialize_as_null(self):
        components = schema.LevelEntityData().to_json()["Components"]
        assert set(components) == {
            "Renderable",
            "Collider",
            "Rigidbody",
            "Interactable",
            "Agent",
            "SpriteAnimation",
            "ParticleEmitter",
            "AudioEmitter",
        }
        assert all(value is None for value in components.values())


class TestEnumsSerializeByName:
    def test_shape_type(self):
        shape = schema.ColliderShapeData(shape_type=schema.PhysicsShapeType.CAPSULE)
        assert shape.to_json()["ShapeType"] == "Capsule"

    def test_body_type(self):
        body = schema.RigidbodyComponentData(body_type=schema.PhysicsBodyType.DYNAMIC)
        assert body.to_json()["BodyType"] == "Dynamic"

    def test_particle_kind(self):
        emitter = schema.ParticleEmitterComponentData(kind=schema.ParticleRenderKind.VOXEL)
        assert emitter.to_json()["Kind"] == "Voxel"

    def test_enum_values_are_strings_not_integers(self):
        """The C# writer needs an explicit converter per enum or it silently emits integers.
        Here they are strings by construction, which is the point of the ``str`` subclass."""
        text = writer.dumps(schema.ColliderShapeData().to_json())
        assert '"ShapeType": "Box"' in text


class TestNormalization:
    def test_sprite_frame_count_zero_means_the_full_grid(self):
        sprite = schema.SpriteAnimationComponentData(columns=4, rows=2, frame_count=0)
        sprite.validate_and_normalize()
        assert sprite.frame_count == 8

    def test_sprite_frame_count_is_clamped_to_the_grid(self):
        sprite = schema.SpriteAnimationComponentData(columns=2, rows=2, frame_count=99)
        sprite.validate_and_normalize()
        assert sprite.frame_count == 4

    def test_sprite_rejects_non_positive_fps(self):
        sprite = schema.SpriteAnimationComponentData(fps=0.0)
        sprite.validate_and_normalize()
        assert sprite.fps == 10.0

    def test_particles_are_capped_at_the_runtime_buffer_size(self):
        """64 is the runtime's per-emitter snapshot buffer, not a style choice -- exceeding
        it would overrun the layout."""
        emitter = schema.ParticleEmitterComponentData(max_particles=1000)
        emitter.validate_and_normalize()
        assert emitter.max_particles == 64

    def test_particle_seed_zero_is_replaced(self):
        """Seed 0 would put every emitter in the scene on the RNG's degenerate stream."""
        emitter = schema.ParticleEmitterComponentData(seed=0)
        emitter.validate_and_normalize()
        assert emitter.seed == 1

    def test_particle_end_size_falls_back_to_start_size(self):
        emitter = schema.ParticleEmitterComponentData(start_size=2.0, end_size=0.0)
        emitter.validate_and_normalize()
        assert emitter.end_size == 2.0

    def test_particle_spread_is_clamped_to_a_half_sphere(self):
        emitter = schema.ParticleEmitterComponentData(spread_degrees=400.0)
        emitter.validate_and_normalize()
        assert emitter.spread_degrees == 180.0

    def test_audio_attenuation_scale_rejects_non_positive(self):
        """Zero collapses the authored falloff curve, so the emitter is either silent
        everywhere or audible everywhere -- both read as a broken sound, not a bad number."""
        for bad in (0.0, -3.0):
            emitter = schema.AudioEmitterComponentData(attenuation_scale=bad)
            emitter.validate_and_normalize()
            assert emitter.attenuation_scale == 1.0

    def test_audio_attenuation_scale_keeps_an_authored_value(self):
        emitter = schema.AudioEmitterComponentData(attenuation_scale=4.0)
        emitter.validate_and_normalize()
        assert emitter.attenuation_scale == 4.0


class TestAudioEmitter:
    def test_field_order_and_names_match_the_csharp_record(self):
        """System.Text.Json writes properties in declaration order, so the key ORDER is part
        of the contract, not just the key set -- see this module's docstring."""
        emitter = schema.AudioEmitterComponentData(
            start_event="Play_Arcade_Bed",
            stop_event="Stop_Arcade_Bed",
            attenuation_scale=2.5,
        )
        assert list(emitter.to_json()) == [
            "StartEvent",
            "StopEvent",
            "PlayOnStart",
            "Is3D",
            "AttenuationScale",
        ]

    def test_defaults_match_the_csharp_defaults(self):
        assert schema.AudioEmitterComponentData().to_json() == {
            "StartEvent": None,
            "StopEvent": None,
            "PlayOnStart": True,
            "Is3D": True,
            "AttenuationScale": 1.0,
        }


class TestEnvironment:
    def test_no_sun_sentinel_is_out_of_cosine_range(self):
        """2.0 can never be a dot product, so the runtime's sun-disk branch cannot trigger."""
        environment = schema.EnvironmentData()
        assert environment.to_json()["SkySunSizeCos"] == 2.0
        assert environment.to_json()["SkySunAngleMaxCos"] == 2.0

    def test_ambient_sh_is_null_unless_skybox(self):
        assert schema.EnvironmentData().to_json()["AmbientSh"] is None

    def test_ambient_sh_is_27_floats_when_present(self):
        environment = schema.EnvironmentData(ambient_sh=[0.0] * 27)
        assert len(environment.to_json()["AmbientSh"]) == 27


def test_full_document_is_valid_json():
    document = schema.LevelData(
        camera=schema.CameraData(),
        entities=[schema.LevelEntityData(id="Ground")],
    )
    document.ensure_lighting_state().lights.append(schema.SceneLightData(id="Sun", type="Directional"))
    parsed = json.loads(writer.dumps(document.to_json()))
    assert parsed["Entities"][0]["Id"] == "Ground"
    assert parsed["Lighting"]["States"][0]["Lights"][0]["Type"] == "Directional"
