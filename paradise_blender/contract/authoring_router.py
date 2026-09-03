"""The contract's own normalization of two authored payloads, applied on the way out.

This used to be a router: a mirror of ``AuthoredComponentRouter`` that placed each engine
component in its typed slot. Schema v5 deleted the slots, so every payload now rides verbatim
under its id and the only thing left to do here is what the slots used to do implicitly --
clamp and derive the fields whose rules live on the typed record (an emitter's frame count from
its grid, an audio emitter's attenuation). Nothing on the READING side calls
``ValidateAndNormalize``, so if the editor did not do it, nobody would.

Payloads arrive as the WIRE dicts :func:`.authoring.build_payload` produces -- schema defaults
already filled, enums as member names, colors as ``{r,g,b,a}`` -- so this module only
normalizes shapes, never invents values.

No ``bpy`` import: pure data, unit-tested standalone.
"""

from __future__ import annotations

from typing import Any

from . import component_ids
from .color import Color32
from .schema import (
    AudioEmitterComponentData,
    ParticleEmitterComponentData,
)

__all__ = ["ROUTED_IDS", "normalize"]

AGENT = component_ids.AGENT
RIGIDBODY = component_ids.RIGIDBODY
AUDIO_EMITTER = component_ids.AUDIO_EMITTER
PARTICLE_EMITTER = component_ids.PARTICLE_EMITTER

#: The ids this module still has something to say about: the two :func:`normalize` clamps on the
#: way out. Everything else rides verbatim.
#:
#: Identity was the third, and the only one that was not a component on the wire — it was spread
#: onto the entity record's own fields, because it was what the entity WAS rather than something
#: it had. Schema v5 deleted the record; the component went with it, and with it the last reason
#: this module needed a dispatch rather than a pair of clamps.
ROUTED_IDS = frozenset({AUDIO_EMITTER, PARTICLE_EMITTER})


def normalize(component_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """A payload with the contract's own normalization applied, or the payload unchanged.

    Payloads ride verbatim now, which would have quietly dropped this: two components clamp and
    derive fields (an emitter's frame count from its grid, an audio emitter's attenuation), and
    NOTHING calls ValidateAndNormalize on the reading side — the methods exist in the contract but
    no runtime path invokes them. It has always been the editor's job, so it stays one, done here
    on the way out instead of on the way into a slot.

    Round-tripped through the typed record rather than reimplemented on the dict: the rules live
    on the record, and a second copy operating on raw keys would drift from them.
    """
    if component_id == AUDIO_EMITTER:
        data = AudioEmitterComponentData(
            start_event=_null_if_blank(payload.get("StartEvent")),
            stop_event=_null_if_blank(payload.get("StopEvent")),
            play_on_start=bool(payload["PlayOnStart"]),
            is_3d=bool(payload["Is3D"]),
            attenuation_scale=float(payload["AttenuationScale"]),
        )
        data.validate_and_normalize()
        return data.to_json()
    if component_id == PARTICLE_EMITTER:
        return _normalized_particles(payload)
    return payload


def _normalized_particles(payload: dict[str, Any]) -> dict[str, Any]:
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
    return data.to_json()


def _null_if_blank(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
