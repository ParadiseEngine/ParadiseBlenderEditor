"""Sprite-animation and particle-emitter components.

The Godot host reads sprite geometry off a ``Sprite3D`` child node and takes only the playback
clock from the authoring script. Blender has no billboard-sprite object type, so the whole
component is authored (see :mod:`..authoring.entity`) and the quad size is derived from the
object's own bounds -- which is what an author modelling a sprite plane would expect.

Spritesheet references follow the same rule as the Godot host: the source image must live
under the data directory's ``sprites/`` folder, and the exported field carries the ``.ktx2``
extension of the sidecar the ingest pass produces. The resolver accepts exactly the set that
pass covers, so an exported sheet field always has a generator behind it -- referencing an
image the pipeline never transcodes would produce a path the runtime cannot load.
"""

from __future__ import annotations

import os

import bpy

from .. import log
from ..contract.color import Color32
from ..contract.schema import (
    ParticleEmitterComponentData,
    ParticleRenderKind,
    SpriteAnimationComponentData,
)
from ..paths import ExportPaths

__all__ = ["build_particle_emitter", "build_sprite_animation", "resolve_sheet_field"]

#: The only directory whose images get KTX2 sidecars, matching the Godot host's ingest pass.
SPRITES_PREFIX = "sprites/"


def build_sprite_animation(obj: bpy.types.Object, paths: ExportPaths) -> SpriteAnimationComponentData:
    props = obj.paradise

    data = SpriteAnimationComponentData(
        sheet=resolve_sheet_field(obj, props.sprite_sheet, paths),
        columns=props.sprite_columns,
        rows=props.sprite_rows,
        frame_count=props.sprite_frame_count,
        fps=props.sprite_fps,
        loop=props.sprite_loop,
        quad_size=_quad_size(obj),
        billboard=props.sprite_billboard,
    )
    data.validate_and_normalize()
    return data


def build_particle_emitter(obj: bpy.types.Object, paths: ExportPaths) -> ParticleEmitterComponentData:
    props = obj.paradise
    kind = (
        ParticleRenderKind.VOXEL if props.particle_kind == "Voxel" else ParticleRenderKind.SPRITE
    )

    data = ParticleEmitterComponentData(
        kind=kind,
        max_particles=props.particle_max_count,
        emit_rate=props.particle_emit_rate,
        lifetime_seconds=props.particle_lifetime,
        initial_speed=props.particle_speed,
        spread_degrees=props.particle_spread_degrees,
        gravity=props.particle_gravity,
        drag=props.particle_drag,
        start_size=props.particle_start_size,
        end_size=props.particle_end_size,
        seed=props.particle_seed,
        color=Color32.from_rgba(*props.particle_color),
        # A voxel emitter renders solid cubes and has no sheet; exporting one would be inert
        # data that implies the wrong render path.
        sheet=(
            resolve_sheet_field(obj, props.particle_sheet, paths)
            if kind == ParticleRenderKind.SPRITE
            else None
        ),
        columns=props.particle_sheet_columns,
        rows=props.particle_sheet_rows,
        frame_count=props.particle_sheet_frame_count,
        fps=props.particle_sheet_fps,
    )
    data.validate_and_normalize()
    return data


def resolve_sheet_field(
    obj: bpy.types.Object, sheet_path: str, paths: ExportPaths
) -> str | None:
    """Map an authored spritesheet image to its ``.ktx2`` contract field.

    Returns ``None`` (with a warning) when the image is outside ``sprites/``, because the
    KTX2 sidecar pass only covers that directory -- so the runtime could never load it, and
    exporting the reference would turn a fixable authoring mistake into a silent runtime miss.
    """
    if not sheet_path or not sheet_path.strip():
        return None

    absolute = os.path.abspath(bpy.path.abspath(sheet_path))
    field = paths.data_relative_field(absolute)

    if field is None or not field.startswith(SPRITES_PREFIX):
        log.warn(
            f"Entity '{obj.name}' references spritesheet '{sheet_path}' outside "
            f"'{paths.data_dir}/sprites'. The KTX2 sidecar pass only covers that directory, so "
            "the runtime could never load it. Move the image there. The sheet is not exported."
        )
        return None

    return os.path.splitext(field)[0] + ".ktx2"


def _quad_size(obj: bpy.types.Object) -> tuple[float, float]:
    """World size of the sprite quad, in meters.

    Derived from the object's world-space dimensions: X is width, and height comes from
    Blender's Z (the contract's Y, i.e. up). A flat plane modelled in the XY ground plane has
    zero Z extent, so its Y extent is used instead -- otherwise every ground-plane sprite
    would export a zero-height quad and render as nothing.
    """
    width, depth, height = obj.dimensions
    if height <= 1e-6:
        height = depth
    return (max(width, 1e-6), max(height, 1e-6))
