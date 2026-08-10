"""Python mirror of ``Paradise.Export.Data.LevelDocument``.

One dataclass per C# record, with the **same field order** -- System.Text.Json writes
properties in declaration order, so preserving it keeps a Blender export diffable against a
Godot export of the same scene.

Conventions carried over from the C# side, all load-bearing:

* Every field is emitted, including nulls (``DefaultIgnoreCondition = Never``).
* Vectors/quaternions serialize as flat float arrays; matrices as 16 floats, column-major,
  translation at indices 12/13/14 (see :mod:`.matrix`).
* ``Color32`` serializes as ``{"r","g","b","a"}`` with 8-bit-quantized channels.
* Enums serialize **by name** (``"Box"``, ``"Dynamic"``). The C# writer needs an explicit
  converter registration per enum or it silently emits integers; here they are plain ``str``
  subclasses so the mistake is not expressible.
* Defaults match the C# defaults exactly, so a field the Blender addon does not author yet
  still round-trips to the same value the Godot addon would produce.

Values are stored as plain Python floats and quantized to float32 at serialization time by
:func:`.writer.f32_repr` -- see that module for why.

No ``bpy`` import: this module is pure data and is unit-tested standalone.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from .color import Color32
from .matrix import flatten_column_major, quat_to_json, vec2_to_json, vec3_to_json

__all__ = [
    "SCHEMA_VERSION",
    "AgentComponentData",
    "CameraData",
    "ColliderComponentData",
    "ColliderShapeData",
    "EntityComponentsData",
    "EntityInteractableComponentData",
    "EntityParentData",
    "EnvironmentData",
    "LevelData",
    "LevelEntityData",
    "LevelMaterialData",
    "LightingData",
    "LightingStateData",
    "NavMeshAgentData",
    "ParticleEmitterComponentData",
    "ParticleRenderKind",
    "PhysicsBodyType",
    "PhysicsShapeType",
    "PrefabOverrideData",
    "PrefabTemplateData",
    "RenderableComponentData",
    "RigidbodyComponentData",
    "SSceneLightData",
    "SceneLightData",
    "SpriteAnimationComponentData",
]

# LevelData.CurrentSchemaVersion. v2 is the source-GLB pipeline: Renderable.Mesh references a
# shared GLB under data/ rather than a per-entity bake. Bump only in lockstep with the engine.
SCHEMA_VERSION = 2

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


def _matrix_json(m) -> list[float] | None:  # axes.Mat4 | None
    return None if m is None else flatten_column_major(m)


# --------------------------------------------------------------------------------------
# Components
# --------------------------------------------------------------------------------------


@dataclass
class RenderableComponentData:
    """Mesh reference. ``mesh`` is a GLB path relative to ``data/``.

    Contract rule the exporter must uphold: the GLB's primitive order equals
    ``LevelEntityData.materials`` slot order, and a null slot means the GLB's own embedded
    material wins. Textures inside the GLB must be KTX2 -- the engine reader rejects PNG/JPEG.
    """

    mesh: str | None = None
    mesh_node: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"Mesh": self.mesh, "MeshNode": self.mesh_node}


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
            # NavObstacle is a Unity-era carving feature with no Godot or Blender authoring
            # equivalent; both hosts emit null. Kept in the shape so the key set matches.
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
class AgentComponentData:
    move_speed: float = 1.4
    acceleration: float = 40.0
    idle_clip: str | None = None
    walk_clip: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "MoveSpeed": self.move_speed,
            "Acceleration": self.acceleration,
            "IdleClip": self.idle_clip,
            "WalkClip": self.walk_clip,
        }


@dataclass
class EntityInteractableComponentData:
    display_name: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"DisplayName": self.display_name}


@dataclass
class SpriteAnimationComponentData:
    sheet: str | None = None
    columns: int = 1
    rows: int = 1
    frame_count: int = 0
    fps: float = 10.0
    loop: bool = True
    quad_size: Vec2 = (1.0, 1.0)
    billboard: bool = True

    def validate_and_normalize(self) -> None:
        """Port of ``SpriteAnimationComponentData.ValidateAndNormalize``.

        Both hosts normalize *before* writing so the runtime never has to defend against
        out-of-range authoring, and so the two exporters cannot disagree on what e.g.
        ``FrameCount = 0`` means.
        """
        self.columns = max(1, self.columns)
        self.rows = max(1, self.rows)
        cells = self.columns * self.rows
        count = cells if self.frame_count <= 0 else self.frame_count
        self.frame_count = min(max(count, 1), cells)
        self.fps = self.fps if _finite(self.fps) and self.fps > 0.0 else 10.0
        width, height = self.quad_size
        self.quad_size = (
            width if _finite(width) and width > 0.0 else 1.0,
            height if _finite(height) and height > 0.0 else 1.0,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "Sheet": self.sheet,
            "Columns": self.columns,
            "Rows": self.rows,
            "FrameCount": self.frame_count,
            "Fps": self.fps,
            "Loop": self.loop,
            "QuadSize": vec2_to_json(self.quad_size),
            "Billboard": self.billboard,
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
        """Port of ``ParticleEmitterComponentData.ValidateAndNormalize``.

        The 64-particle ceiling is the runtime's per-emitter buffer size, not a style choice --
        exceeding it would overrun the snapshot layout.
        """
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
class EntityComponentsData:
    renderable: RenderableComponentData | None = None
    collider: ColliderComponentData | None = None
    rigidbody: RigidbodyComponentData | None = None
    interactable: EntityInteractableComponentData | None = None
    agent: AgentComponentData | None = None
    sprite_animation: SpriteAnimationComponentData | None = None
    particle_emitter: ParticleEmitterComponentData | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "Renderable": _opt(self.renderable),
            "Collider": _opt(self.collider),
            "Rigidbody": _opt(self.rigidbody),
            "Interactable": _opt(self.interactable),
            "Agent": _opt(self.agent),
            "SpriteAnimation": _opt(self.sprite_animation),
            "ParticleEmitter": _opt(self.particle_emitter),
        }


# --------------------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------------------


@dataclass
class EntityParentData:
    id: str = ""
    bone_path: str | None = None
    bone_index: int = -1

    def to_json(self) -> dict[str, Any]:
        return {"Id": self.id, "BonePath": self.bone_path, "BoneIndex": self.bone_index}


@dataclass
class PrefabOverrideData:
    """Per-instance override tracking.

    Neither the Godot nor the Blender host populates this: Godot has no API equivalent to
    Unity's ``PrefabUtility.GetPropertyModifications``, and Blender's collection instances
    cannot override interior properties at all (an override there means a full library
    override, i.e. a different datablock). Emitted empty to keep the key set stable.
    """

    transform: bool = False
    material_slots: list[int] = field(default_factory=list)
    colliders: list[str] = field(default_factory=list)
    metadata: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "Transform": self.transform,
            "MaterialSlots": list(self.material_slots),
            "Colliders": list(self.colliders),
            "Metadata": list(self.metadata),
        }


@dataclass
class LevelEntityData:
    id: str = ""
    entity_guid: uuid.UUID = field(default_factory=lambda: uuid.UUID(int=0))
    stable_id: str | None = None
    display_name: str | None = None
    kind: str | None = None
    spawn_phase: str | None = None
    is_active: bool = True
    prefab: str | None = None
    prefab_asset_path: str | None = None
    nearest_instance_root: str | None = None
    prefab_guid: str | None = None
    prefab_asset_type: str | None = None
    initial_animation: str | None = None
    parent: EntityParentData | None = None
    local_position: Vec3 = ZERO3
    local_rotation: Quat = IDENTITY_QUAT
    local_scale: Vec3 = ONE3
    local_matrix: Any = None
    world_matrix: Any = None
    materials: list[str | None] = field(default_factory=list)
    overrides: PrefabOverrideData = field(default_factory=PrefabOverrideData)
    components: EntityComponentsData = field(default_factory=EntityComponentsData)

    def to_json(self) -> dict[str, Any]:
        return {
            "Id": self.id,
            # System.Text.Json's default Guid form: lowercase, hyphenated ("D" format).
            "EntityGuid": str(self.entity_guid),
            "StableId": self.stable_id,
            "DisplayName": self.display_name,
            "Kind": self.kind,
            "SpawnPhase": self.spawn_phase,
            "IsActive": self.is_active,
            "Prefab": self.prefab,
            "PrefabAssetPath": self.prefab_asset_path,
            "NearestInstanceRoot": self.nearest_instance_root,
            "PrefabGuid": self.prefab_guid,
            "PrefabAssetType": self.prefab_asset_type,
            "InitialAnimation": self.initial_animation,
            "Parent": _opt(self.parent),
            "LocalPosition": vec3_to_json(self.local_position),
            "LocalRotation": quat_to_json(self.local_rotation),
            "LocalScale": vec3_to_json(self.local_scale),
            "LocalMatrix": _matrix_json(self.local_matrix),
            "WorldMatrix": _matrix_json(self.world_matrix),
            "Materials": list(self.materials),
            "Overrides": self.overrides.to_json(),
            "Components": self.components.to_json(),
        }


@dataclass
class PrefabTemplateData:
    display_name: str | None = None
    prefab: str | None = None
    prefab_asset_path: str | None = None
    prefab_guid: str | None = None
    prefab_asset_type: str | None = None
    materials: list[str | None] = field(default_factory=list)
    entities: list[LevelEntityData] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "DisplayName": self.display_name,
            "Prefab": self.prefab,
            "PrefabAssetPath": self.prefab_asset_path,
            "PrefabGuid": self.prefab_guid,
            "PrefabAssetType": self.prefab_asset_type,
            "Materials": list(self.materials),
            "Entities": [e.to_json() for e in self.entities],
        }


# --------------------------------------------------------------------------------------
# Scene-level
# --------------------------------------------------------------------------------------


@dataclass
class CameraData:
    position: Vec3 = ZERO3
    rotation: Vec3 = ZERO3
    orthographic_size: float = 10.0
    background_color: Color32 = field(default_factory=lambda: Color32.from_rgba(0.72, 0.69, 0.67))

    def to_json(self) -> dict[str, Any]:
        return {
            "Position": vec3_to_json(self.position),
            # Euler DEGREES, in the contract basis (see axes.convert_euler_zyx_degrees).
            "Rotation": vec3_to_json(self.rotation),
            "OrthographicSize": self.orthographic_size,
            "BackgroundColor": self.background_color.to_json(),
        }


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


# Historical alias guard: an early draft of this module exported the name with a typo. Kept so
# a stale import fails loudly at the import site rather than silently shadowing.
SSceneLightData = SceneLightData


@dataclass
class EnvironmentData:
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
class LightingStateData:
    name: str = "Default"
    environment: EnvironmentData = field(default_factory=EnvironmentData)
    lights: list[SceneLightData] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "Name": self.name,
            "Environment": self.environment.to_json(),
            "Lights": [light.to_json() for light in self.lights],
        }


@dataclass
class LightingData:
    active_state: str = "Default"
    states: list[LightingStateData] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "ActiveState": self.active_state,
            "States": [state.to_json() for state in self.states],
        }


@dataclass
class NavMeshAgentData:
    speed: float = 2.0
    angular_speed: float = 720.0
    acceleration: float = 40.0

    def to_json(self) -> dict[str, Any]:
        return {
            "Speed": self.speed,
            "AngularSpeed": self.angular_speed,
            "Acceleration": self.acceleration,
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
    """Root document written to ``data/scenes/<Scene>.json``."""

    schema_version: int = SCHEMA_VERSION
    camera: CameraData | None = None
    lighting: LightingData | None = None
    nav_mesh_agent: NavMeshAgentData | None = None
    interactables: list[Any] = field(default_factory=list)
    entities: list[LevelEntityData] = field(default_factory=list)
    nav_mesh_file: str | None = None
    # Materials are written as separate documents under data/materials/; this in-document list
    # stays empty in both hosts (the Godot exporter never populates it either), but the key
    # must exist or the engine reader sees a shape change.
    materials: list[Any] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "SchemaVersion": self.schema_version,
            "Camera": _opt(self.camera),
            "Lighting": _opt(self.lighting),
            "NavMeshAgent": _opt(self.nav_mesh_agent),
            "Interactables": [i.to_json() for i in self.interactables],
            "Entities": [e.to_json() for e in self.entities],
            "NavMeshFile": self.nav_mesh_file,
            "Materials": [m.to_json() for m in self.materials],
        }

    def ensure_lighting_state(self) -> LightingStateData:
        """Get (creating if needed) the first lighting state.

        Mirrors ``SceneDataExporter.EnsureLightingState``: the contract supports several named
        lighting states, but both authoring hosts emit exactly one, called "Default".
        """
        if self.lighting is None:
            self.lighting = LightingData(active_state="Default")
        if not self.lighting.states:
            self.lighting.states.append(LightingStateData(name="Default"))
        return self.lighting.states[0]


def _opt(value: Any) -> Any:
    return None if value is None else value.to_json()


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def _positive_or(value: float, fallback: float) -> float:
    return value if _finite(value) and value > 0.0 else fallback
