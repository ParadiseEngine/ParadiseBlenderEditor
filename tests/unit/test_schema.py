"""Tests for the document schema mirror.

These assert *shape*, not values: key names, key order, which keys exist, and that enums
serialize by name. A shape mismatch is what breaks the engine's reader; a value mismatch is a
scene-authoring question.
"""

from __future__ import annotations

import json

from paradise_blender.contract import component_ids, matrix, schema, writer


class TestLevelData:
    def test_key_order_matches_the_csharp_declaration_order(self):
        assert list(schema.LevelData().to_json()) == [
            "SchemaVersion",
            "Entities",
        ]

    def test_schema_version_defaults_to_the_pinned_version(self):
        assert schema.LevelData().schema_version == schema.SCHEMA_VERSION == 5

    def test_an_object_is_a_bare_component_array(self):
        """The whole of schema v5, in one assertion. An object has no keys of its own, so there is
        no key order for it to get wrong and no field a host can forget to write."""
        document = schema.LevelData(entities=[schema.EntityComponentsData()])
        assert document.to_json()["Entities"] == [[]]


class TestRenderableComponentData:
    def test_key_order(self):
        """Same guard the entity has, for the record the slots moved to in contract v4.

        System.Text.Json writes properties in declaration order, so this list IS the wire shape --
        and a Blender export has to stay diffable against a Godot one written by the C# record.
        """
        assert list(schema.RenderableComponentData().to_json()) == [
            "Mesh",
            "MeshNode",
        ]

    def test_slots_are_written_even_when_empty(self):
        """Empty, not absent. The engine's reader distinguishes "no overrides" from "key missing"
        only if the key is always written.

        On MaterialsComponentData since v5 -- the slots left the renderable, because they are not
        geometry."""
        assert schema.MaterialsComponentData().to_json()["Slots"] == []

    def test_a_null_slot_survives(self):
        """A null slot MEANS something -- the GLB's own embedded material wins for that primitive
        -- so it cannot be compacted away. Slot order is the contract: dropping one shifts every
        override after it onto the wrong primitive."""
        materials = schema.MaterialsComponentData(
            slots=["materials/a.json", None, "materials/c.json"])
        assert materials.to_json()["Slots"] == ["materials/a.json", None, "materials/c.json"]


class TestPlacementComponents:
    """The two components every host writes for every object it emits.

    They are what the entity record used to state as fields — a name and a world matrix — and the
    reason they are components is that the record is gone. Their key order is pinned for the same
    reason every other component's is: System.Text.Json writes properties in declaration order, so
    this list IS the wire shape, and a Blender export has to stay diffable against a Godot one.
    """

    def test_name_key_order(self):
        assert list(schema.NameComponentData().to_json()) == ["Value"]

    def test_transform_key_order(self):
        assert list(schema.TransformComponentData().to_json()) == ["World"]

    def test_transform_writes_sixteen_floats_column_major(self):
        """Translation at 12/13/14, which is what "column-major, column-vector" means on the wire.
        An object written with the translation at 3/7/11 loads at the origin, silently."""
        world = matrix.trs((1.0, 2.0, 3.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0))
        flat = schema.TransformComponentData(world=world).to_json()["World"]
        assert len(flat) == 16
        assert flat[12:15] == [1.0, 2.0, 3.0]


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
    ground = schema.EntityComponentsData()
    ground.add(schema.AuthoredComponentData(
        id=component_ids.NAME,
        type="Paradise.Export.Data.NameComponentData",
        data=schema.NameComponentData(value="Ground").to_json()))

    sun = schema.EntityComponentsData()
    sun.add(schema.AuthoredComponentData(
        id=component_ids.LIGHT,
        type="Paradise.Export.Data.SceneLightData",
        data=schema.SceneLightData(id="Sun", type="Directional").to_json()))

    document = schema.LevelData(entities=[ground, sun])
    parsed = json.loads(writer.dumps(document.to_json()))

    assert parsed["Entities"][0][0]["Data"]["Value"] == "Ground"
    assert parsed["Entities"][1][0]["Data"]["Type"] == "Directional"
