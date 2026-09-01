"""Rendering a picture of each prefab, for the Asset Browser to show.

A catalogue asset is an EMPTY -- :mod:`catalogue` explains at length why it has to be -- and an
empty has nothing to render, so Blender's own ``lib_id_generate_preview`` gives every prefab in the
library the same generic icon. The way out is that **an ID's preview image is independent of the
ID's content**: ``ed.lib_id_load_custom_preview`` puts an arbitrary PNG on the empty. So the
catalogue keeps its one-lightweight-object-per-prefab shape, and the browser still shows the asset.

What gets rendered is produced by :func:`materialize.load.load_document` -- the same code path that
materializes a prefab when you open one. That is the point rather than a convenience: the thumbnail
is then BY CONSTRUCTION what a drop produces, including nested prefab expansion and the authored
object colour, and there is no second answer to "what does this prefab look like" to drift from.

**Run this before the catalogue's factory reset, never after.** Rendering imports GLBs, and those
datablocks must not be alive when ``prefabs.blend`` is saved -- a catalogue carrying the project's
geometry would be hundreds of megabytes and would defeat the empty in the first place.
:func:`catalogue.build` orders the two phases around exactly that reset.
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

#: Thumbnail edge, in pixels. 256 rather than Blender's native 128 because the Asset Browser's
#: thumbnail slider goes well past 128 and a stretched preview reads as a broken asset. Verified to
#: survive the save/reload round trip at full size -- a clamp would show up as a 128 read back --
#: and it costs about 1.6 KB per asset in the catalogue.
SIZE = 256

#: Where the camera sits relative to the subject: front-right and above, the three-quarter view
#: Blender's own asset previews use. Fixed rather than chosen per asset so a library of props reads
#: as one set; an angle that varied with the geometry would make two similar props look different
#: for a reason that has nothing to do with the props.
DIRECTION = Vector((0.7, -1.0, 0.55)).normalized()

#: Multiples of the subject's bounding radius: how far back the camera sits, and how much of the
#: frame it fills. The gap between them is the margin.
DISTANCE = 4.0
FRAME = 2.2

#: Preferred engine, and what to fall back to when it will not run.
#:
#: EEVEE shows the asset as it ships -- ``assets/`` GLBs reference their PNG sources, which Blender
#: reads, so materials and textures come through. It wants a GPU, which a headless CI box may not
#: have; Workbench draws a silhouette on anything. Falling back beats failing, but it IS a degraded
#: thumbnail, so it says so once rather than quietly.
ENGINE = "BLENDER_EEVEE"
FALLBACK_ENGINE = "BLENDER_WORKBENCH"


def previews_directory(project_root: str) -> str:
    """Where a project's rendered thumbnails live. Derived, and disposable like the catalogue."""
    # Imported here rather than at module scope: catalogue imports this module, and the constant
    # is the only thing wanted back from it.
    from .catalogue import CATALOGUE_RELATIVE

    return os.path.join(project_root, CATALOGUE_RELATIVE, "previews")


def preview_path(destination: str, relative: str) -> str:
    """The PNG for the prefab at assets-relative ``relative``.

    Mirrors the prefab's path under ``assets/`` rather than flattening to its filename, for the
    reason ``ProjectLayout.blend_for`` gives: ``levels/test.prefab`` and ``prefabs/test.prefab``
    are two documents with one stem, and a stem-keyed file would hand the second one the first
    one's picture.
    """
    return os.path.join(destination, os.path.splitext(relative)[0] + ".png")


# --------------------------------------------------------------------------------------
# Staleness
# --------------------------------------------------------------------------------------
#
# The manifest beside each PNG records the files that render READ -- reported by the loader, never
# guessed at here -- and their mtimes. A rebuild re-renders unless every one is still exactly as
# recorded. Anything unreadable, missing or unrecognised re-renders: under `.editor/` a wasted
# render costs a second, and a wrongly-kept thumbnail costs an author trusting a picture of an
# asset that has since changed.


def _manifest_path(png: str) -> str:
    return os.path.splitext(png)[0] + ".json"


def _mtimes(sources) -> dict[str, float]:
    """Stamp each source. A file that is not there stamps as 0.0 rather than dropping out.

    Dropping it would leave the manifest looking complete, and the file's RETURN would then not
    count as a change -- a prefab whose missing mesh was restored would keep its meshless picture.
    """
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
    # Equality rather than ">=": a file reverted to an older version is a change too, and a
    # thumbnail is cheap enough that there is no reason to be clever here.
    return all(current.get(path) == stamp for path, stamp in sources.items())


def _write_manifest(png: str, sources) -> None:
    with open(_manifest_path(png), "w", encoding="utf-8") as handle:
        json.dump({"size": SIZE, "sources": _mtimes(sources)}, handle, indent=1, sort_keys=True)


# --------------------------------------------------------------------------------------
# The render scene
# --------------------------------------------------------------------------------------


def _setup(scene: bpy.types.Scene, engine: str) -> bpy.types.Object:
    """Give ``scene`` render settings, a key light and a camera; return the camera."""
    scene.render.engine = engine
    scene.render.resolution_x = scene.render.resolution_y = SIZE
    scene.render.resolution_percentage = 100
    # Transparent, so the thumbnail composites onto whatever the browser draws behind it in
    # whatever theme the author uses. A baked-in background would be wrong in half of them.
    scene.render.film_transparent = True
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"

    if engine == FALLBACK_ENGINE:
        _shade_flat(scene)

    camera_data = bpy.data.cameras.new("ParadiseThumbnail")
    # Orthographic: framing is then an exact function of the bounding radius, and a mountain and a
    # teacup get identical treatment. Perspective would foreshorten large props unpredictably.
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
    """Workbench settings for the fallback path.

    Object colour rather than material: with no shading to go on, the per-instance colour
    ``load._apply_authored_colour`` writes is the only thing left that tells one graybox from
    another.
    """
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "OBJECT"


def _bounds(scene: bpy.types.Scene):
    """World-space ``(center, radius)`` of everything visible, or ``None`` when nothing is.

    Walks the evaluated depsgraph's instances rather than ``scene.objects``, because a materialized
    prefab displays geometry through COLLECTION INSTANCES: the objects in the scene are empties
    whose own bound boxes are degenerate, and the geometry sits one level down in a collection
    excluded from the view layer. ``object_instances`` flattens precisely that.
    """
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
    # Derived from the subject rather than fixed: a 0.1 m prop and a 500 m level both have to fall
    # between the planes, and any fixed near plane clips one of them.
    camera.data.clip_start = max(radius * 0.01, 1e-4)
    camera.data.clip_end = radius * (DISTANCE + 4.0)


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def render_all(
    layout: project.ProjectLayout, prefabs, destination: str | None = None
) -> tuple[dict[str, str], list[str]]:
    """Render a thumbnail for each prefab path in ``prefabs``.

    Returns ``({assets-relative prefab path: png path}, warnings)``. Only prefabs that HAVE a
    picture appear in the mapping; one that could not be read, or that holds no geometry to look
    at, is left out and its asset keeps the generic icon. That is the honest outcome -- a trigger
    volume genuinely has nothing to show, and inventing a placeholder for it would make it
    indistinguishable from a prop whose render failed.
    """
    destination = destination or previews_directory(layout.root)
    warnings: list[str] = []
    rendered: dict[str, str] = {}
    engine = ENGINE

    for path in prefabs:
        relative = layout.relative(path)
        png = preview_path(destination, relative)

        if _is_current(png):
            rendered[relative] = png
            continue

        try:
            wrote, engine = _render_one(path, layout, png, engine, warnings)
        except Exception as error:
            # One malformed asset must not abort the batch and silently skip every prefab after
            # it. The known failures are handled inside; this is for the unknown ones.
            warnings.append(
                f"{relative}: thumbnail failed unexpectedly "
                f"({type(error).__name__}: {error}); the asset keeps the generic icon"
            )
            continue

        if wrote:
            rendered[relative] = png

    return rendered, warnings


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

    # Each prefab renders in a FRESH file: the previous one's imported GLBs would otherwise pile
    # up, and by the last prefab the process would be holding the whole project's geometry.
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
