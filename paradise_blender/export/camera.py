"""Blender camera -> ``CameraData``."""

from __future__ import annotations

import bpy

from ..contract import axes
from ..contract.color import Color32
from ..contract.schema import CameraData

__all__ = ["export_camera", "find_camera"]


def find_camera(scene: bpy.types.Scene) -> bpy.types.Object | None:
    """The camera the contract should describe.

    Prefers the scene's active camera -- the one a Blender author considers "the" camera and
    the one F12 renders through. Falls back to the first camera object so a scene with
    cameras but no active one still exports something usable.
    """
    if scene.camera is not None and scene.camera.type == "CAMERA":
        return scene.camera
    return next((obj for obj in scene.objects if obj.type == "CAMERA"), None)


def export_camera(obj: bpy.types.Object, background: Color32 | None = None) -> CameraData:
    """Convert a Blender camera object to contract camera data.

    Both the position and the Euler rotation are rebased from Blender's Z-up to the contract's
    Y-up (see :mod:`..contract.axes`); this is the one place the contract uses Euler angles
    rather than a matrix, so the conversion goes through the matrix and back.
    """
    matrix = obj.matrix_world
    position = axes.convert_point(tuple(matrix.translation))
    rotation = axes.convert_euler_zyx_degrees(tuple(matrix.to_euler("XYZ")))

    data = CameraData(position=position, rotation=rotation)

    camera = obj.data
    if camera.type == "ORTHO":
        # The contract's OrthographicSize is a HALF-height (matching Unity's orthographicSize
        # and Godot's Camera3D.Size). Blender's ortho_scale is the FULL span of the larger
        # viewport axis, so it halves. Getting this wrong doubles or halves the visible world.
        data.orthographic_size = camera.ortho_scale / 2.0
    else:
        # A perspective camera has no orthographic size; the contract keeps the field, and the
        # runtime uses its own --fov for perspective. Leave the default rather than inventing
        # a value derived from the FOV, which would be meaningless at any particular depth.
        data.orthographic_size = CameraData().orthographic_size

    if background is not None:
        data.background_color = background

    return data
