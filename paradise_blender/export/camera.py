"""Blender cameras -> the HostCamera field set, posed through ``decompose_contract`` so a camera
and a mesh cannot disagree about which way is up."""

from __future__ import annotations

import math

import bpy

from .transform import decompose_contract

__all__ = ["host_camera_values"]


def host_camera_values(obj: bpy.types.Object) -> dict | None:
    """HostCamera fields, or ``None`` for a non-camera rather than an invented 50° perspective."""
    if obj.type != "CAMERA" or obj.data is None:
        return None
    camera = obj.data
    position, rotation, _scale, _matrix = decompose_contract(obj.matrix_world)
    orthographic = camera.type == "ORTHO"
    return {
        "Projection": "Orthographic" if orthographic else "Perspective",
        # ``angle`` is the vertical FOV in radians; HostCamera.Fov is degrees.
        "Fov": math.degrees(camera.angle),
        "OrthographicSize": float(camera.ortho_scale),
        "Near": float(camera.clip_start),
        "Far": float(camera.clip_end),
        "Position": list(position),
        "Rotation": list(rotation),
    }
