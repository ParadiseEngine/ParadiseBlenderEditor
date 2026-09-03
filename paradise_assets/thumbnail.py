"""Rendering a picture of each prefab for the Asset Browser.

A catalogue asset is an empty, so ``lib_id_generate_preview`` gives every prefab the same icon;
``ed.lib_id_load_custom_preview`` puts an arbitrary PNG on it instead. The render goes through
``load.load_document``, so the thumbnail is by construction what a drop produces. Run BEFORE
the catalogue's factory reset: the imported GLBs must not be alive when ``prefabs.blend`` is
saved, or the catalogue carries the project's geometry.
"""

from __future__ import annotations

import json
import math
import os

import bpy
from mathutils import Vector

from .document import project
from .document.prefab import PrefabDocumentError
from .document.prefab import loads as parse_document
from .materialize.load import load_document

__all__ = ["SIZE", "preview_path", "previews_directory", "render_all"]

#: 256, not Blender's native 128: the browser's slider goes past 128 and a stretched preview
#: reads as a broken asset. Verified to survive the save/reload round trip; ~1.6 KB per asset.
SIZE = 256

#: Blender's own three-quarter view, fixed so a library of props reads as one set.
DIRECTION = Vector((0.7, -1.0, 0.55)).normalized()

DISTANCE = 4.0
FRAME = 2.2

#: EEVEE shows textures but wants a GPU a headless CI box may lack; Workbench draws a
#: silhouette on anything. The fallback is degraded, so it says so once.
ENGINE = "BLENDER_EEVEE"
FALLBACK_ENGINE = "BLENDER_WORKBENCH"


def previews_directory(project_root: str) -> str:
    """Where a project's rendered thumbnails live. Derived, and disposable like the catalogue."""
    from .catalogue import CATALOGUE_RELATIVE

    return os.path.join(project_root, CATALOGUE_RELATIVE, "previews")


def preview_path(destination: str, relative: str) -> str:
    """The PNG for a prefab, mirroring its path under ``assets/``: two documents can share a stem."""
    return os.path.join(destination, os.path.splitext(relative)[0] + ".png")


# Staleness: the manifest beside each PNG records the files the render READ (reported by the
# loader, never guessed here) and their mtimes. Anything unrecognised re-renders; a wasted
# render costs a second, a wrongly kept thumbnail costs trust.


def _manifest_path(png: str) -> str:
    return os.path.splitext(png)[0] + ".json"


def _mtimes(sources) -> dict[str, float]:
    """Stamp each source; a missing file stamps 0.0 rather than dropping out, so its return
    counts as a change."""
    stamps: dict[str, float] = {}
    for path in sources:
        try:
            stamps[path] = os.path.getmtime(path)
        except OSError:
            stamps[path] = 0.0
    return stamps


def _is_current(png: str) -> bool:
    """Whether ``png`` still matches every file the render that produced it read."""
    if not os.path.isfile(png):
        return False
    try:
        with open(_manifest_path(png), encoding="utf-8") as handle:
            recorded = json.load(handle)
    except (OSError, ValueError):
        return False

    if not isinstance(recorded, dict) or recorded.get("size") != SIZE:
        return False
    sources = recorded.get("sources")
    if not isinstance(sources, dict) or not sources:
        return False

    current = _mtimes(sources.keys())
    # Equality, not ">=": a file reverted to an older version is a change too.
    return all(current.get(path) == stamp for path, stamp in sources.items())


def _write_manifest(png: str, sources) -> None:
    with open(_manifest_path(png), "w", encoding="utf-8") as handle:
        json.dump({"size": SIZE, "sources": _mtimes(sources)}, handle, indent=1, sort_keys=True)


def _setup(scene: bpy.types.Scene, engine: str) -> bpy.types.Object:
    """Give ``scene`` render settings, a key light and a camera; return the camera."""
    scene.render.engine = engine
    scene.render.resolution_x = scene.render.resolution_y = SIZE
    scene.render.resolution_percentage = 100
    # Transparent: a baked-in background is wrong in half the themes.
    scene.render.film_transparent = True
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"

    if engine == FALLBACK_ENGINE:
        _shade_flat(scene)

    camera_data = bpy.data.cameras.new("ParadiseThumbnail")
    # Orthographic, so framing is an exact function of the bounding radius.
    camera_data.type = "ORTHO"
    camera = bpy.data.objects.new("ParadiseThumbnail", camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera

    sun_data = bpy.data.lights.new("ParadiseThumbnailKey", type="SUN")
    sun_data.energy = 3.0
    sun = bpy.data.objects.new("ParadiseThumbnailKey", sun_data)
    scene.collection.objects.link(sun)
    sun.rotation_euler = (math.radians(55.0), 0.0, math.radians(35.0))

    # A dim ambient world, so faces turned away from the key read as shape rather than as black.
    world = bpy.data.worlds.new("ParadiseThumbnailWorld")
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    if background is not None:
        background.inputs[1].default_value = 0.6
    scene.world = world

    return camera


def _shade_flat(scene: bpy.types.Scene) -> None:
    """Workbench fallback: object colour, the only thing that tells one graybox from another."""
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "OBJECT"


def _bounds(scene: bpy.types.Scene):
    """World-space ``(center, radius)`` of everything visible, via ``object_instances``: the
    scene objects are empties instancing collections, and their own bound boxes are degenerate."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    points: list[Vector] = []
    for instance in depsgraph.object_instances:
        obj = instance.object
        if obj is None or obj.type not in {"MESH", "CURVE", "SURFACE", "FONT", "META"}:
            continue
        matrix = instance.matrix_world
        points.extend(matrix @ Vector(corner) for corner in obj.bound_box)

    if not points:
        return None

    low = Vector(min(p[axis] for p in points) for axis in range(3))
    high = Vector(max(p[axis] for p in points) for axis in range(3))
    # Floored, so a flat plane or a single point still frames instead of dividing by zero.
    return (low + high) / 2.0, max((high - low).length / 2.0, 1e-4)


def _aim(camera: bpy.types.Object, center: Vector, radius: float) -> None:
    """Point ``camera`` at the subject and frame it."""
    camera.location = center + DIRECTION * (radius * DISTANCE)
    direction = (center - camera.location).normalized()
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    camera.data.ortho_scale = radius * FRAME
    # Clip planes from the subject: any fixed near plane clips a 0.1 m prop or a 500 m level.
    camera.data.clip_start = max(radius * 0.01, 1e-4)
    camera.data.clip_end = radius * (DISTANCE + 4.0)


def render_all(
    layout: project.ProjectLayout, prefabs, destination: str | None = None
) -> tuple[dict[str, str], list[str]]:
    """Render each prefab; returns ``({prefab path: png path}, warnings)``. A prefab with no
    geometry keeps the generic icon: a placeholder would be indistinguishable from a failed render."""
    destination = destination or previews_directory(layout.root)
    warnings: list[str] = []
    rendered: dict[str, str] = {}
    engine = _preferred_engine()
    if engine != ENGINE:
        warnings.append(
            f"{ENGINE} needs libEGL, which is not on this machine; using {engine}. "
            "Thumbnails show the shape but not the materials."
        )

    for path in prefabs:
        relative = layout.relative(path)
        png = preview_path(destination, relative)

        if _is_current(png):
            rendered[relative] = png
            continue

        try:
            wrote, engine = _render_one(path, layout, png, engine, warnings)
        except Exception as error:
            # One malformed asset must not abort the batch.
            warnings.append(
                f"{relative}: thumbnail failed unexpectedly "
                f"({type(error).__name__}: {error}); the asset keeps the generic icon"
            )
            continue

        if wrote:
            rendered[relative] = png

    return rendered, warnings


def _preferred_engine() -> str:
    """EEVEE except on Linux with no libEGL, where EEVEE SIGABRTs the process before any
    Python fallback can run."""
    import ctypes.util
    import sys

    if sys.platform != "linux":
        return ENGINE
    if ctypes.util.find_library("EGL") or ctypes.util.find_library("egl"):
        return ENGINE
    return FALLBACK_ENGINE


def _render_one(
    path: str,
    layout: project.ProjectLayout,
    png: str,
    engine: str,
    warnings: list[str],
) -> tuple[bool, str]:
    """Render one prefab. Returns ``(wrote a png, the engine to use for the next one)``."""
    relative = layout.relative(path)

    try:
        with open(path, encoding="utf-8") as handle:
            document = parse_document(handle.read(), path)
    except (OSError, PrefabDocumentError) as error:
        warnings.append(f"{relative}: {error}")
        return False, engine

    # A fresh file per prefab, or imported GLBs pile up into the whole project's geometry.
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    camera = _setup(scene, engine)

    result = load_document(scene, document, path, layout)
    box = _bounds(scene)
    if box is None:
        warnings.append(
            f"{relative}: nothing to render ({result.objects} object(s), none with geometry), "
            "so it keeps the generic icon"
        )
        return False, engine

    _aim(camera, *box)
    os.makedirs(os.path.dirname(png), exist_ok=True)
    scene.render.filepath = png

    try:
        bpy.ops.render.render(write_still=True)
    except RuntimeError as error:
        if engine == FALLBACK_ENGINE:
            warnings.append(f"{relative}: render failed ({error})")
            return False, engine

        # Said once, on the first failure, and every later prefab goes straight to the fallback.
        warnings.append(
            f"{ENGINE} could not render ({error}); falling back to {FALLBACK_ENGINE} for every "
            "thumbnail. Those show the shape but not the materials -- this usually means Blender "
            "has no GPU available on this machine."
        )
        scene.render.engine = FALLBACK_ENGINE
        _shade_flat(scene)
        try:
            bpy.ops.render.render(write_still=True)
        except RuntimeError as second:
            warnings.append(f"{relative}: render failed on both engines ({second})")
            return False, FALLBACK_ENGINE
        _write_manifest(png, result.sources)
        return True, FALLBACK_ENGINE

    _write_manifest(png, result.sources)
    return True, engine
