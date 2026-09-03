"""Tests for the document schema mirror.

These assert *shape*, not values: key names, key order, which keys exist, and that enums
serialize by name. A shape mismatch is what breaks the engine's reader; a value mismatch is a
scene-authoring question.
"""

from __future__ import annotations

import json

from paradise_blender.contract import component_ids, schema, well_known, writer


class TestLevelData:
    def test_key_order_matches_the_csharp_declaration_order(self):
        assert list(schema.LevelData().to_json()) == [
            "SchemaVersion",
            "Entities",
        ]

    def test_schema_version_defaults_to_the_pinned_version(self):
        assert schema.LevelData().schema_version == schema.SCHEMA_VERSION == 6

    def test_an_object_is_a_bare_component_array(self):
        """The whole of schema v5, in one assertion. An object has no keys of its own, so there is
        no key order for it to get wrong and no field a host can forget to write."""
        document = schema.LevelData(entities=[schema.EntityComponentsData()])
        assert document.to_json()["Entities"] == [[]]


class TestMaterialsComponentData:
    def test_slots_are_written_even_when_empty(self):
        """Empty, not absent. The engine's reader distinguishes "no overrides" from "key missing"
        only if the key is always written."""
        assert schema.MaterialsComponentData().to_json()["Slots"] == []

    def test_a_null_slot_survives(self):
        """A null slot MEANS something -- the GLB's own embedded material wins for that primitive
        -- so it cannot be compacted away. Slot order is the contract: dropping one shifts every
        override after it onto the wrong primitive."""
        materials = schema.MaterialsComponentData(
            slots=["materials/a.json", None, "materials/c.json"])
        assert materials.to_json()["Slots"] == ["materials/a.json", None, "materials/c.json"]


class TestRetiredRecords:
    def test_the_v5_records_the_engine_dropped_are_gone(self):
        """v6 declares no authored components; a record here that no game can receive is a trap
        (#25). Placement is the format's own meta / transform payloads, in well_known."""
        for name in ("NameComponentData", "TransformComponentData", "RenderableComponentData",
                     "AgentComponentData", "EntityInteractableComponentData",
                     "SpriteAnimationComponentData", "SSceneLightData"):
            assert not hasattr(schema, name), name
        for name in ("NAME", "TRANSFORM", "INTERACTABLE"):
            assert not hasattr(component_ids, name), name


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
    ground = schema.EntityComponentsData()
    ground.add(schema.AuthoredComponentData(
        id=well_known.META_ID,
        type=well_known.META_TYPE,
        data=well_known.meta_payload(
            guid="6f0f2a1c-8b3d-4e57-9a24-0d5c7e1b3f88", name="Ground", parent=None)))

    sun = schema.EntityComponentsData()
    sun.add(schema.AuthoredComponentData(
        id=component_ids.LIGHT,
        type="Paradise.Export.Data.SceneLightData",
        data=schema.SceneLightData(id="Sun", type="Directional").to_json()))

    document = schema.LevelData(entities=[ground, sun])
    parsed = json.loads(writer.dumps(document.to_json()))

    assert parsed["Entities"][0][0]["Data"]["Name"] == "Ground"
    assert parsed["Entities"][1][0]["Data"]["Type"] == "Directional"
