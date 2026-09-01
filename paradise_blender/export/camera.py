"""Blender cameras -> the HostCamera field set.

A camera is authored by pointing at a Camera object, the same way a light is authored by
pointing at a lamp. The lens (projection, FOV, clip) comes off the datablock; where it stands
and which way it looks come off the object's world pose, rebased through
:func:`.transform.decompose_contract` so a camera and a mesh cannot disagree about which way
is up.
"""

from __future__ import annotations

import math

import bpy

from .transform import decompose_contract

__all__ = ["host_camera_values"]


def host_camera_values(obj: bpy.types.Object) -> dict | None:
    """A camera as the HostCamera field set, in the storage shape ``build_payload`` consumes.

    Returns ``None`` when ``obj`` is not a Camera, so a slot pointed at a mesh warns and stays
    unauthored rather than inventing a 50° perspective at the origin.
    """
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
