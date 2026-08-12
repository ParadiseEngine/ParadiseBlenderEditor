"""Audio-emitter component.

A sound source is authored as an ordinary Blender object with the audio flag set: its origin is
where the sound comes from, and the object's own transform is what places it -- there is no
Blender datablock to read settings off, unlike a mesh or a light, so the whole component lives
in the object's Paradise properties.

**Event names are exported verbatim and are never validated here.** The events exist only inside
the audio project (a Wwise ``.wproj``), which Blender has no visibility into, and the middleware
resolves a name to an id by hashing it at bank-generation time. So the exporter cannot tell a
typo from an event that has not been authored yet, and refusing to export an unknown name would
make the addon depend on a tool it cannot see. The failure mode is a silent emitter, which is
also what the audio tool itself does with an unknown event.
"""

from __future__ import annotations

import bpy

from ..contract.schema import AudioEmitterComponentData

__all__ = ["build_audio_emitter"]


def build_audio_emitter(obj: bpy.types.Object) -> AudioEmitterComponentData:
    props = obj.paradise

    data = AudioEmitterComponentData(
        # Empty strings become null: the contract distinguishes "no event authored" from
        # "an event named the empty string", and only the first is meaningful.
        start_event=_optional(props.audio_start_event),
        stop_event=_optional(props.audio_stop_event),
        play_on_start=props.audio_play_on_start,
        is_3d=props.audio_is_3d,
        attenuation_scale=props.audio_attenuation_scale,
    )
    data.validate_and_normalize()
    return data


def _optional(value: str) -> str | None:
    stripped = value.strip() if value else ""
    return stripped or None
