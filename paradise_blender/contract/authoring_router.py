"""Puts an authored engine-component payload where the runtime expects to find it.

Python mirror of ``Paradise.Export.Data.AuthoredComponentRouter``, for the subset of engine
components this host authors through the schema. An editor knows only ids and JSON — it has no
idea that the rigidbody component belongs in ``Components.Rigidbody`` — so the mapping lives on
the contract, where both halves can see it.

Components are named in words below and spelled as GUIDs in the code: identity is
``0c068bf4-…``, and the ids come from :mod:`.component_ids` so there is one place they are
written down. A component's id says nothing about what it is any more, which is exactly why the
prose has to.

The subset is deliberate, and the split is this host's, not the schema's:

* **Routed here** (plain fields, authored in the Components panel): identity, agent, rigidbody,
  audio-emitter, particle-emitter.
* **Host-owned** (this host derives them from real Blender data, so authoring them as a form
  would fight the pipeline): renderable (the mesh datablock), collider / interactable (the
  collider empties), light (the lamp datablock). The schema marks light and sprite-animation as
  host-baked anyway (component-level ``authoredBy``).

Payloads arrive as the WIRE dicts :func:`.authoring.build_payload` produces — schema defaults
already filled, enums as member names, colors as ``{r,g,b,a}`` — so this module only maps
shapes, never invents values.

No ``bpy`` import: pure data, unit-tested standalone.
"""

from __future__ import annotations

from typing import Any

from . import component_ids
from .color import Color32
from .schema import (
    AgentComponentData,
    AudioEmitterComponentData,
    LevelEntityData,
    ParticleEmitterComponentData,
    RigidbodyComponentData,
)

__all__ = ["ROUTED_IDS", "apply"]

IDENTITY = component_ids.IDENTITY
AGENT = component_ids.AGENT
RIGIDBODY = component_ids.RIGIDBODY
AUDIO_EMITTER = component_ids.AUDIO_EMITTER
PARTICLE_EMITTER = component_ids.PARTICLE_EMITTER

#: The engine ids this router places. Anything else is either a game's own component (the
#: caller carries it in ``Components.Custom``) or an engine component this host does not
#: author as a form (the caller warns).
ROUTED_IDS = frozenset({IDENTITY, AGENT, RIGIDBODY, AUDIO_EMITTER, PARTICLE_EMITTER})


def apply(entity: LevelEntityData, component_id: str, payload: dict[str, Any]) -> bool:
    """Apply one authored engine component to an entity. False when the id is not routed."""
    if component_id == IDENTITY:
        _apply_identity(entity, payload)
    elif component_id == AGENT:
        entity.components.agent = AgentComponentData(
            move_speed=float(payload["MoveSpeed"]),
            acceleration=float(payload["Acceleration"]),
            idle_clip=_null_if_blank(payload.get("IdleClip")),
            walk_clip=_null_if_blank(payload.get("WalkClip")),
        )
    elif component_id == RIGIDBODY:
        # Authored values travel verbatim — the "zero the mass on a static body" rule belongs
        # to the DERIVED rigidbody the exporter emits alongside colliders, not to something an
        # author typed in deliberately.
        entity.components.rigidbody = RigidbodyComponentData(
            body_type=str(payload["BodyType"]),
            mass=float(payload["Mass"]),
            linear_damping=float(payload["LinearDamping"]),
            restitution=float(payload["Restitution"]),
            friction=float(payload["Friction"]),
            layer=int(payload["Layer"]),
            layer_name=payload.get("LayerName") or "",
        )
    elif component_id == AUDIO_EMITTER:
        data = AudioEmitterComponentData(
            start_event=_null_if_blank(payload.get("StartEvent")),
            stop_event=_null_if_blank(payload.get("StopEvent")),
            play_on_start=bool(payload["PlayOnStart"]),
            is_3d=bool(payload["Is3D"]),
            attenuation_scale=float(payload["AttenuationScale"]),
        )
        data.validate_and_normalize()
        entity.components.audio_emitter = data
    elif component_id == PARTICLE_EMITTER:
        _apply_particles(entity, payload)
    else:
        return False
    return True


def _apply_identity(entity: LevelEntityData, payload: dict[str, Any]) -> None:
    """Spread across the entity's own fields — identity is what an entity IS, not something it
    has, matching the C# router. Blank overridables (DisplayName, SpawnPhase, Prefab) leave the
    exporter's own values alone; here Prefab joins that list because ``model_path`` is
    host-level data the exporter already resolved, and the C# router never faces that."""
    entity.kind = _null_if_blank(payload.get("Kind")) or "Prop"
    entity.is_active = bool(payload["IsActive"])
    entity.initial_animation = _null_if_blank(payload.get("InitialAnimation"))
    if _null_if_blank(payload.get("Prefab")) is not None:
        entity.prefab = payload["Prefab"]
    if _null_if_blank(payload.get("DisplayName")) is not None:
        entity.display_name = payload["DisplayName"]
    if _null_if_blank(payload.get("SpawnPhase")) is not None:
        entity.spawn_phase = payload["SpawnPhase"]


def _apply_particles(entity: LevelEntityData, payload: dict[str, Any]) -> None:
    color = payload.get("Color") or {}
    data = ParticleEmitterComponentData(
        kind=str(payload["Kind"]),
        max_particles=int(payload["MaxParticles"]),
        emit_rate=float(payload["EmitRate"]),
        lifetime_seconds=float(payload["LifetimeSeconds"]),
        initial_speed=float(payload["InitialSpeed"]),
        spread_degrees=float(payload["SpreadDegrees"]),
        gravity=float(payload["Gravity"]),
        drag=float(payload["Drag"]),
        start_size=float(payload["StartSize"]),
        end_size=float(payload["EndSize"]),
        seed=int(payload["Seed"]),
        # The authored color arrives display-encoded ({r,g,b,a} from the picker), exactly what
        # the contract's Color32 stores — pass through, never re-encode.
        color=Color32.from_rgba(
            float(color.get("r", 1.0)), float(color.get("g", 1.0)),
            float(color.get("b", 1.0)), float(color.get("a", 1.0))),
        # Sheet is a host-baked asset reference the schema marks authoredBy=asset; this host
        # does not bake it yet, so the payload never carries it and the field stays None. A
        # Voxel emitter needs none; a Sprite emitter without one renders untextured quads.
        sheet=None,
        columns=int(payload["Columns"]),
        rows=int(payload["Rows"]),
        frame_count=int(payload["FrameCount"]),
        fps=float(payload["Fps"]),
    )
    data.validate_and_normalize()
    entity.components.particle_emitter = data


def _null_if_blank(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
