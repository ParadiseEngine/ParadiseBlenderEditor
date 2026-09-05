"""Python mirror of ``Paradise.Export.Data.LevelDocument``.

Same field order as the C# records: System.Text.Json writes declaration order, and a Blender
export must stay diffable against a Godot one. Every field is emitted, nulls included
(``DefaultIgnoreCondition = Never``). Enums are ``str`` subclasses serialized by name, so the
C# mistake of an unregistered converter emitting integers is not expressible here. Defaults
match C# exactly. Floats are quantized to float32 by :func:`.writer.f32_repr`.

Since v6 the engine declares no authored components of its own, so a record here is not "the
engine's": it is the shape this host DERIVES from Blender data (a collider list, a rigidbody, a
lamp, the environment, material slots) for a game that declares a component under the same id.
Identity and placement are the format's own ``meta`` / ``transform`` payloads (well_known.py),
not records. The v5 records the engine dropped (name, world transform, renderable, agent,
interactable, sprite animation) are gone with it (#25).

No ``bpy`` import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .color import Color32
from .matrix import quat_to_json, vec2_to_json, vec3_to_json

__all__ = [
    "SCHEMA_VERSION",
    "AudioEmitterComponentData",
    "AuthoredComponentData",
    "ColliderComponentData",
    "ColliderShapeData",
    "EntityComponentsData",
    "EnvironmentData",
    "LevelData",
    "LevelMaterialData",
    "MaterialsComponentData",
    "ParticleEmitterComponentData",
    "ParticleRenderKind",
    "PhysicsBodyType",
    "PhysicsShapeType",
    "RigidbodyComponentData",
    "SceneLightData",
]

# LevelData.CurrentSchemaVersion; bump only in lockstep with the engine. v6: the engine declares
# no authored components (ids resolve against the game's schema), name/transform are the
# format's `meta`/`transform` payloads (well_known.py), and the hierarchy is back -- a document
# carries local TRS plus a parent guid, which is why export/placement.py exists.
SCHEMA_VERSION = 6

Vec3 = tuple[float, float, float]
Quat = tuple[float, float, float, float]
Vec2 = tuple[float, float]

ZERO3: Vec3 = (0.0, 0.0, 0.0)
ONE3: Vec3 = (1.0, 1.0, 1.0)
IDENTITY_QUAT: Quat = (0.0, 0.0, 0.0, 1.0)


class PhysicsBodyType(str):
    """``Paradise.Export.Data.PhysicsBodyType`` -- serialized by name."""

    NONE = "None"
    STATIC = "Static"
    KINEMATIC = "Kinematic"
    DYNAMIC = "Dynamic"


class PhysicsShapeType(str):
    """``Paradise.Export.Data.PhysicsShapeType`` -- serialized by name."""

    BOX = "Box"
    SPHERE = "Sphere"
    CAPSULE = "Capsule"


class ParticleRenderKind(str):
    """``Paradise.Export.Data.ParticleRenderKind`` -- serialized by name."""

    SPRITE = "Sprite"
    VOXEL = "Voxel"


@dataclass
class ColliderShapeData:
    id: str | None = None
    path: str | None = None
    is_static: bool = False
    layer: int = 0
    layer_name: str | None = None
    is_trigger: bool = False
    shape_type: str = PhysicsShapeType.BOX
    local_center: Vec3 = ZERO3
    local_rotation: Quat = IDENTITY_QUAT
    size: Vec3 = ZERO3
    radius: float = 0.0
    height: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "Id": self.id,
            "Path": self.path,
            "IsStatic": self.is_static,
            "Layer": self.layer,
            "LayerName": self.layer_name,
            "IsTrigger": self.is_trigger,
            "ShapeType": str(self.shape_type),
            "LocalCenter": vec3_to_json(self.local_center),
            "LocalRotation": quat_to_json(self.local_rotation),
            "Size": vec3_to_json(self.size),
            "Radius": self.radius,
            "Height": self.height,
            # No host authors NavObstacle; both emit null so the key set matches.
            "NavObstacle": None,
        }


@dataclass
class ColliderComponentData:
    colliders: list[ColliderShapeData] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"Colliders": [c.to_json() for c in self.colliders]}


@dataclass
class RigidbodyComponentData:
    body_type: str = PhysicsBodyType.NONE
    mass: float = 1.0
    linear_damping: float = 0.2
    restitution: float = 0.2
    friction: float = 0.5
    layer: int = 0
    layer_name: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "BodyType": str(self.body_type),
            "Mass": self.mass,
            "LinearDamping": self.linear_damping,
            "Restitution": self.restitution,
            "Friction": self.friction,
            "Layer": self.layer,
            "LayerName": self.layer_name,
        }


@dataclass
class ParticleEmitterComponentData:
    kind: str = ParticleRenderKind.SPRITE
    max_particles: int = 64
    emit_rate: float = 8.0
    lifetime_seconds: float = 1.5
    initial_speed: float = 2.0
    spread_degrees: float = 25.0
    gravity: float = -9.8
    drag: float = 0.0
    start_size: float = 0.25
    end_size: float = 0.25
    seed: int = 1
    color: Color32 = field(default_factory=lambda: Color32.from_rgba(1.0, 1.0, 1.0))
    sheet: str | None = None
    columns: int = 1
    rows: int = 1
    frame_count: int = 0
    fps: float = 0.0

    def validate_and_normalize(self) -> None:
        """Port of ``ParticleEmitterComponentData.ValidateAndNormalize``. The 64-particle
        ceiling is the runtime's per-emitter buffer size; exceeding it overruns the snapshot."""
        self.max_particles = min(max(self.max_particles, 1), 64)
        self.emit_rate = _positive_or(self.emit_rate, 8.0)
        self.lifetime_seconds = _positive_or(self.lifetime_seconds, 1.5)
        self.initial_speed = (
            self.initial_speed if _finite(self.initial_speed) and self.initial_speed >= 0.0 else 2.0
        )
        self.spread_degrees = (
            min(max(self.spread_degrees, 0.0), 180.0) if _finite(self.spread_degrees) else 25.0
        )
        self.gravity = self.gravity if _finite(self.gravity) else -9.8
        self.drag = self.drag if _finite(self.drag) and self.drag >= 0.0 else 0.0
        self.start_size = _positive_or(self.start_size, 0.25)
        self.end_size = self.end_size if _finite(self.end_size) and self.end_size > 0.0 else self.start_size
        # Seed 0 would make every emitter in the scene share the RNG's degenerate stream.
        self.seed = 1 if self.seed == 0 else (self.seed & 0xFFFFFFFF)
        self.columns = max(1, self.columns)
        self.rows = max(1, self.rows)
        cells = self.columns * self.rows
        count = cells if self.frame_count <= 0 else self.frame_count
        self.frame_count = min(max(count, 1), cells)
        self.fps = self.fps if _finite(self.fps) and self.fps >= 0.0 else 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "Kind": str(self.kind),
            "MaxParticles": self.max_particles,
            "EmitRate": self.emit_rate,
            "LifetimeSeconds": self.lifetime_seconds,
            "InitialSpeed": self.initial_speed,
            "SpreadDegrees": self.spread_degrees,
            "Gravity": self.gravity,
            "Drag": self.drag,
            "StartSize": self.start_size,
            "EndSize": self.end_size,
            "Seed": self.seed,
            "Color": self.color.to_json(),
            "Sheet": self.sheet,
            "Columns": self.columns,
            "Rows": self.rows,
            "FrameCount": self.frame_count,
            "Fps": self.fps,
        }


@dataclass
class AudioEmitterComponentData:
    """Mirror of ``AudioEmitterComponentData``. Events travel as names, not hashed ids:
    resolving at export would pin the scene to one bank build; the runtime hashes instead."""

    start_event: str | None = None
    stop_event: str | None = None
    play_on_start: bool = True
    is_3d: bool = True
    attenuation_scale: float = 1.0

    def validate_and_normalize(self) -> None:
        """Port of ``AudioEmitterComponentData.ValidateAndNormalize``. A non-positive scale
        collapses the attenuation curve and reads as a broken sound, not a bad number."""
        self.attenuation_scale = _positive_or(self.attenuation_scale, 1.0)

    def to_json(self) -> dict[str, Any]:
        return {
            "StartEvent": self.start_event,
            "StopEvent": self.stop_event,
            "PlayOnStart": self.play_on_start,
            "Is3D": self.is_3d,
            "AttenuationScale": self.attenuation_scale,
        }


@dataclass
class AuthoredComponentData:
    """One authored component: a stable id and an opaque payload (a plain dict, mirroring the
    C# ``JsonElement``; the engine cannot name the type and neither does this)."""

    id: str = ""

    #: CLR name from the schema, verbatim; the engine's fallback when :attr:`id` fails to
    #: resolve. Optional on the wire, so omitted rather than sent empty.
    type: str | None = None

    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        document: dict[str, Any] = {"Id": self.id}
        if self.type:
            document["Type"] = self.type
        document["Data"] = dict(self.data)
        return document


@dataclass
class EntityComponentsData:
    """Every component authored on an entity, one list (schema v3 and later)."""

    components: list[AuthoredComponentData] = field(default_factory=list)

    #: Where :meth:`add_engine` looks type names up. Never serialized.
    data_dir: str | None = None

    def add(self, component: AuthoredComponentData) -> None:
        self.components.append(component)

    def add_engine(self, component_id: str, payload) -> None:
        """Add a typed record's payload; the type name comes from the game's dumped schema and
        a schema that cannot answer is an error (:func:`.component_ids.engine_type_name`)."""
        from . import component_ids

        if self.data_dir is None:
            raise ValueError(
                "EntityComponentsData was built without a data_dir, so the CLR type name for "
                f"engine component {component_id} cannot be resolved. Construct it with the "
                "export's data directory (ExportPaths.data_dir).")

        self.components.append(AuthoredComponentData(
            id=component_id,
            type=component_ids.engine_type_name(component_id, self.data_dir),
            data=payload.to_json(),
        ))

    def find(self, component_id: str) -> AuthoredComponentData | None:
        """The entry for one id, or None."""
        for component in self.components:
            if component.id == component_id:
                return component
        return None

    def to_json(self) -> list[dict[str, Any]]:
        return [component.to_json() for component in self.components]


@dataclass
class MaterialsComponentData:
    """Material overrides, one per GLB primitive. A null entry keeps the GLB's own material;
    dropping one would shift every override after it onto the wrong primitive."""

    slots: list[str | None] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"Slots": list(self.slots)}


@dataclass
class SceneLightData:
    id: str = ""
    type: str = ""
    position: Vec3 = ZERO3
    direction: Vec3 = ZERO3
    color: Color32 = field(default_factory=lambda: Color32.from_rgba(1.0, 1.0, 1.0))
    enabled: bool = True
    intensity: float = 1.0
    use_color_temperature: bool = False
    color_temperature: float = 6570.0
    range: float = 0.0
    attenuation_exponent: float = 1.0
    spot_angle: float = 0.0
    inner_spot_angle: float = 0.0
    area_size: Vec2 = (0.0, 0.0)
    shadows_enabled: bool = False
    shadow_type: str = ""
    shadow_strength: float = 1.0
    specular: float = 0.5
    size: float = 0.0
    layer_mask: int = 0
    rendering_layer_mask: int = 0
    group: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "Id": self.id,
            "Type": self.type,
            "Position": vec3_to_json(self.position),
            "Direction": vec3_to_json(self.direction),
            "Color": self.color.to_json(),
            "Enabled": self.enabled,
            "Intensity": self.intensity,
            "UseColorTemperature": self.use_color_temperature,
            "ColorTemperature": self.color_temperature,
            "Range": self.range,
            "AttenuationExponent": self.attenuation_exponent,
            "SpotAngle": self.spot_angle,
            "InnerSpotAngle": self.inner_spot_angle,
            "AreaSize": vec2_to_json(self.area_size),
            "ShadowsEnabled": self.shadows_enabled,
            "ShadowType": self.shadow_type,
            "ShadowStrength": self.shadow_strength,
            "Specular": self.specular,
            "Size": self.size,
            "LayerMask": self.layer_mask,
            "RenderingLayerMask": self.rendering_layer_mask,
            "Group": self.group,
        }


@dataclass
class EnvironmentData:
    """Scene lighting, a component on an object of its own since v5."""

    #: Shadow map resolution in texels; None leaves the renderer's default.
    shadow_map_size: int | None = None
    #: PCF disk radius in shadow texels; None leaves the renderer's default.
    shadow_blur: float | None = None
    ambient_mode: str = "Color"
    ambient_color: Color32 = field(default_factory=lambda: Color32.from_rgba(0.5, 0.52, 0.56))
    ambient_equator_color: Color32 = field(default_factory=lambda: Color32.from_rgba(0.5, 0.52, 0.56))
    ambient_ground_color: Color32 = field(default_factory=lambda: Color32.from_rgba(0.2, 0.19, 0.18))
    # 27 floats (9 RGB L2 coefficients) or None when ambient_mode != "Skybox".
    ambient_sh: list[float] | None = None
    sky_reflections: bool = False
    # 2 is an out-of-range sentinel for a cosine: "no sun disk". Not a magic number to tidy up.
    sky_sun_size_cos: float = 2.0
    sky_sun_angle_max_cos: float = 2.0
    sky_sun_inv_curve: float = 24.0
    exposure: float = 1.0
    ambient_energy: float = 1.0
    has_background: bool = False
    background_color: Color32 = field(default_factory=lambda: Color32.from_rgba(0.5, 0.52, 0.56))
    sky_gradient: bool = False
    sky_top_color: Color32 = field(default_factory=lambda: Color32.from_rgba(0.03, 0.024, 0.016))
    sky_horizon_color: Color32 = field(default_factory=lambda: Color32.from_rgba(0.2, 0.2, 0.21))
    sky_ground_bottom_color: Color32 = field(default_factory=lambda: Color32.from_rgba(0.03, 0.024, 0.016))
    sky_ground_horizon_color: Color32 = field(default_factory=lambda: Color32.from_rgba(0.2, 0.2, 0.21))
    sky_sky_curve_inv: float = 4.0
    sky_ground_curve_inv: float = 30.0
    fog_enabled: bool = False
    fog_color: Color32 = field(default_factory=lambda: Color32.from_rgba(0.5, 0.52, 0.56))
    fog_density: float = 0.0
    ssao_enabled: bool = False
    ssao_radius: float = 1.0
    ssao_intensity: float = 2.0
    ssao_power: float = 1.5
    tonemap_mode: str = "Linear"
    tonemap_exposure: float = 1.0
    tonemap_white: float = 1.0
    glow_enabled: bool = False
    glow_intensity: float = 0.6
    glow_threshold: float = 1.0

    def to_json(self) -> dict[str, Any]:
        return {
            "ShadowMapSize": self.shadow_map_size,
            "ShadowBlur": self.shadow_blur,
            "AmbientMode": self.ambient_mode,
            "AmbientColor": self.ambient_color.to_json(),
            "AmbientEquatorColor": self.ambient_equator_color.to_json(),
            "AmbientGroundColor": self.ambient_ground_color.to_json(),
            "AmbientSh": None if self.ambient_sh is None else list(self.ambient_sh),
            "SkyReflections": self.sky_reflections,
            "SkySunSizeCos": self.sky_sun_size_cos,
            "SkySunAngleMaxCos": self.sky_sun_angle_max_cos,
            "SkySunInvCurve": self.sky_sun_inv_curve,
            "Exposure": self.exposure,
            "AmbientEnergy": self.ambient_energy,
            "HasBackground": self.has_background,
            "BackgroundColor": self.background_color.to_json(),
            "SkyGradient": self.sky_gradient,
            "SkyTopColor": self.sky_top_color.to_json(),
            "SkyHorizonColor": self.sky_horizon_color.to_json(),
            "SkyGroundBottomColor": self.sky_ground_bottom_color.to_json(),
            "SkyGroundHorizonColor": self.sky_ground_horizon_color.to_json(),
            "SkySkyCurveInv": self.sky_sky_curve_inv,
            "SkyGroundCurveInv": self.sky_ground_curve_inv,
            "FogEnabled": self.fog_enabled,
            "FogColor": self.fog_color.to_json(),
            "FogDensity": self.fog_density,
            "SsaoEnabled": self.ssao_enabled,
            "SsaoRadius": self.ssao_radius,
            "SsaoIntensity": self.ssao_intensity,
            "SsaoPower": self.ssao_power,
            "TonemapMode": self.tonemap_mode,
            "TonemapExposure": self.tonemap_exposure,
            "TonemapWhite": self.tonemap_white,
            "GlowEnabled": self.glow_enabled,
            "GlowIntensity": self.glow_intensity,
            "GlowThreshold": self.glow_threshold,
        }


@dataclass
class LevelMaterialData:
    path: str = ""
    name: str = ""
    base_color_factor: Color32 = field(default_factory=lambda: Color32.from_rgba(1.0, 1.0, 1.0))
    base_color_texture: str | None = None
    metallic_factor: float = 1.0
    roughness_factor: float = 1.0
    metallic_roughness_texture: str | None = None
    emissive_factor: Color32 = field(default_factory=lambda: Color32.from_rgba(0.0, 0.0, 0.0))
    emissive_texture: str | None = None
    normal_scale: float = 1.0
    normal_texture: str | None = None
    occlusion_strength: float = 1.0
    occlusion_texture: str | None = None
    #: KHR_texture_transform on the base-colour uv set. The .blend exporter has no source for it
    #: (a material document from a GLB does), so it writes the contract's identity transform.
    base_color_uv_offset: tuple[float, float] = (0.0, 0.0)
    base_color_uv_scale: tuple[float, float] = (1.0, 1.0)
    base_color_uv_rotation: float = 0.0
    alpha_mode: str = "Opaque"
    render_queue: int = -1
    transmission_factor: float = 0.0
    material_kind: str = ""
    emissive_strength: float = 1.0
    noise_scale: float = 1.0
    flow_speed: float = 1.0
    color_a: Color32 = field(default_factory=lambda: Color32.from_rgba(1.0, 1.0, 1.0))
    color_b: Color32 = field(default_factory=lambda: Color32.from_rgba(0.0, 0.0, 0.0))

    def to_json(self) -> dict[str, Any]:
        return {
            "Path": self.path,
            "Name": self.name,
            "BaseColorFactor": self.base_color_factor.to_json(),
            "BaseColorTexture": self.base_color_texture,
            "MetallicFactor": self.metallic_factor,
            "RoughnessFactor": self.roughness_factor,
            "MetallicRoughnessTexture": self.metallic_roughness_texture,
            "EmissiveFactor": self.emissive_factor.to_json(),
            "EmissiveTexture": self.emissive_texture,
            "NormalScale": self.normal_scale,
            "NormalTexture": self.normal_texture,
            "OcclusionStrength": self.occlusion_strength,
            "OcclusionTexture": self.occlusion_texture,
            "BaseColorUvOffset": list(self.base_color_uv_offset),
            "BaseColorUvScale": list(self.base_color_uv_scale),
            "BaseColorUvRotation": self.base_color_uv_rotation,
            "AlphaMode": self.alpha_mode,
            "RenderQueue": self.render_queue,
            "TransmissionFactor": self.transmission_factor,
            "MaterialKind": self.material_kind,
            "EmissiveStrength": self.emissive_strength,
            "NoiseScale": self.noise_scale,
            "FlowSpeed": self.flow_speed,
            "ColorA": self.color_a.to_json(),
            "ColorB": self.color_b.to_json(),
        }


@dataclass
class LevelData:
    """Root document, ``data/scenes/<Scene>.json``: a version and a list of objects, each a
    list of components."""

    schema_version: int = SCHEMA_VERSION
    entities: list[EntityComponentsData] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "SchemaVersion": self.schema_version,
            "Entities": [e.to_json() for e in self.entities],
        }


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def _positive_or(value: float, fallback: float) -> float:
    return value if _finite(value) and value > 0.0 else fallback
