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
    "AudioEmitterComponentData",
    "AuthoredComponentData",
    "ColliderComponentData",
    "ColliderShapeData",
    "EntityComponentsData",
    "EntityInteractableComponentData",
    "EnvironmentData",
    "LevelData",
    "LevelMaterialData",
    "MaterialsComponentData",
    "NameComponentData",
    "ParticleEmitterComponentData",
    "ParticleRenderKind",
    "PhysicsBodyType",
    "PhysicsShapeType",
    "RenderableComponentData",
    "RigidbodyComponentData",
    "SSceneLightData",
    "SceneLightData",
    "SpriteAnimationComponentData",
    "TransformComponentData",
]

# LevelData.CurrentSchemaVersion. Bump only in lockstep with the engine.
#
# v5 reduced an entity to its authored components: an entry under "Entities" is now a bare ARRAY
# of {Id, Type, Data}, not an object with eighteen fields and a Components key. The object's name
# and its world matrix travel as ordinary components (Name, Transform); an object the author
# switched off is one this exporter does not write; the parent link, the prefab provenance and the
# override table are gone outright. So are the document's own blocks -- Camera, Lighting,
# NavMeshAgent, Interactables, Materials -- with the scene's environment becoming a component on
# an object of its own and each lamp becoming an object carrying a Light.
#
# v4 moved an entity's material slots onto its Renderable component; v3 replaced nine named
# component slots with one list. Both are below the engine's floor now.
SCHEMA_VERSION = 5

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
    """Mesh reference and the material slots that index against it.

    ``mesh`` is a GLB path relative to ``data/``. Textures inside the GLB must be KTX2 -- the
    engine reader rejects PNG/JPEG.

    Contract rule the exporter must uphold: the GLB's primitive order equals ``materials`` slot
    order, and a null slot means the GLB's own embedded material wins. ``materials`` lived on the
    ENTITY until v4, which made that rule a statement about two fields nothing held together --
    an entity could carry slots with no mesh to index them against.
    """

    mesh: str | None = None
    mesh_node: str | None = None
    materials: list[str | None] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "Mesh": self.mesh,
            "MeshNode": self.mesh_node,
            # Declaration order matches the C# record: System.Text.Json writes properties in that
            # order, and a Blender export has to stay diffable against a Godot one.
            "Materials": list(self.materials),
        }


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
class AudioEmitterComponentData:
    """A positional sound source. Mirror of ``AudioEmitterComponentData``.

    Events are carried as NAMES, not resolved ids. The audio middleware derives an id by hashing
    the name when banks are generated, so resolving at export time would pin the scene to one
    particular bank build; the runtime hashes instead, which survives regeneration. A name that
    matches nothing simply plays nothing -- event names live only inside the audio project, which
    the exporter cannot see, so a renamed event goes quiet rather than failing the export.
    """

    start_event: str | None = None
    stop_event: str | None = None
    play_on_start: bool = True
    is_3d: bool = True
    attenuation_scale: float = 1.0

    def validate_and_normalize(self) -> None:
        """Port of ``AudioEmitterComponentData.ValidateAndNormalize``.

        A zero or negative scale collapses the authored attenuation curve, leaving the emitter
        either silent everywhere or audible everywhere -- both of which read as a broken sound
        rather than a bad number, so repair rather than trust it.
        """
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
    """One game-defined component riding along with an entity: a stable id and an opaque
    payload.

    ``data`` is a plain dict on purpose -- the mirror of the C# side carrying it as a raw
    ``JsonElement``. The engine cannot name the type (that is the entire point of the
    mechanism), so neither does this mirror: the payload is built by
    :func:`.authoring.build_payload` from the game's authoring schema and travels untouched.
    """

    id: str = ""

    #: Fully qualified CLR name of the record, copied verbatim from the authoring schema. Read by
    #: the engine only when :attr:`id` fails to resolve, but written whenever it is known: a
    #: payload that loaded as nothing tells whoever is reading it precisely nothing without this.
    #: Optional on the wire, so it is omitted rather than sent empty.
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
    """Every component authored on an entity: engine's and game's alike, one list.

    This used to be nine typed slots plus a ``custom`` list, mirroring nine named keys in the
    contract. The contract has one list now (schema v3), so this does too -- and the exporter no
    longer has to know which of two places a component belongs in.

    The typed records above have not gone anywhere: they are still what builds a payload, and
    :meth:`add_engine` serializes one into an entry. What went is the idea that the engine's
    components get a reserved key and a game's do not.
    """

    components: list[AuthoredComponentData] = field(default_factory=list)

    #: Where the game's authoring schema lives, so :meth:`add_engine` can look a component's CLR
    #: type name up in it. Not part of the wire format and never serialized -- :meth:`to_json`
    #: writes only ``components`` -- but it has to be HERE rather than on each call, because the
    #: seven call sites that add an engine component all already hold an ``ExportPaths`` and none
    #: of them should have to think about where a type name comes from.
    data_dir: str | None = None

    def add(self, component: AuthoredComponentData) -> None:
        self.components.append(component)

    def add_engine(self, component_id: str, payload) -> None:
        """One of the ENGINE's components, from the typed record that builds its payload.

        The CLR type name is read out of the game's dumped schema -- which describes the engine's
        components too, because the launcher that dumps it scans its references. See
        :func:`.component_ids.engine_type_name`, including why a schema that cannot answer is an
        error rather than an omitted field.
        """
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
        """The entry for one id, or None. A list has no fixed positions, so a caller that needs to
        read back what it just wrote -- the agent/rigidbody rule does -- asks by id."""
        for component in self.components:
            if component.id == component_id:
                return component
        return None

    def to_json(self) -> list[dict[str, Any]]:
        return [component.to_json() for component in self.components]


# --------------------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# Objects
# --------------------------------------------------------------------------------------
#
# There is no entity dataclass. An object in the document IS its component list -- see
# LevelData.entities -- so what would have been LevelEntityData is an EntityComponentsData and
# nothing else. The two facts every host states about every object it writes are components like
# any other: NameComponentData and TransformComponentData, below.


@dataclass
class NameComponentData:
    """What an object is called.

    For DIAGNOSTICS. A scene is two hundred objects and a runtime refusal that cannot name one
    sends someone counting rows in a JSON file. It is not an identity: nothing resolves anything
    by it, and two objects may share a name.
    """

    value: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"Value": self.value}


@dataclass
class MaterialsComponentData:
    """The materials that override a mesh's own, one per GLB primitive.

    An ENGINE component, and it has to be: a material assignment is Blender's material slots, so it
    is derived by every exporter from the object it is exporting, exactly as the name and the
    transform are. A game cannot own it for the same reason a game cannot own the transform — the
    host that fills it in cannot be made to know a particular game's type.

    A null entry keeps the GLB's own material for that primitive, and dropping one would shift
    every override after it onto the wrong primitive.
    """

    slots: list[str | None] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"Slots": list(self.slots)}


@dataclass
class TransformComponentData:
    """Where an object stands: its world placement, column-major, translation at 12/13/14.

    The one component an exporter writes for every object it emits -- anything that exists is
    somewhere. It replaces the four transform fields the entity record used to carry (local
    position/rotation/scale plus two matrices), which stated the same placement four times and
    let them disagree: a non-uniformly scaled parent made the decomposed three a lossy version of
    the matrix nobody could tell from the exact one.
    """

    world: Any = None

    def to_json(self) -> dict[str, Any]:
        return {"World": _matrix_json(self.world)}


# --------------------------------------------------------------------------------------
# Scene-level
# --------------------------------------------------------------------------------------


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
    """How the scene is lit as a whole -- a COMPONENT on an object of its own since v5.

    It used to be reachable only through a named "active state" in a document-level Lighting
    block: a second addressing scheme for a thing exactly one of which was ever used. The two
    shadow settings moved here with it, because they were on that block for want of anywhere
    else and they are as much part of "how this scene is lit" as the ambient is.
    """

    #: Per-layer shadow map resolution the scene asks its renderer for, in texels. None leaves
    #: the renderer's default in place. Always serialized (null included) to mirror the C#
    #: contract's serialization exactly.
    shadow_map_size: int | None = None
    #: Soft-shadow blur: the PCF disk radius in shadow texels -- the penumbra width of every
    #: shadow edge. None leaves the renderer's default.
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
    """Root document written to ``data/scenes/<Scene>.json``.

    Since v5 it is a version and a list of objects, and an object is a list of components. Every
    other key this used to carry -- Camera, Lighting, NavMeshAgent, Interactables, NavMeshFile,
    Materials -- is gone: three were written by this host and read by nobody, and the two that
    WERE read (the lighting states and the viewport camera) are now components, which is the
    whole point. One rule for the document: a host writes components, a runtime reads components.
    """

    schema_version: int = SCHEMA_VERSION
    #: One entry per object, and an object IS its authored components.
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
