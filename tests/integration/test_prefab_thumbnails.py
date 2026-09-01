"""Rendering Asset Browser thumbnails, and keeping them out of the catalogue.

    blender --background --factory-startup --python tests/integration/test_prefab_thumbnails.py -- <project>

``<project>`` is a directory holding ``assets/project.toml``; it defaults to the workspace's
ShiningPie checkout, because half of what this checks only means anything against real content --
a GLB that actually has geometry, materials Blender actually reads, a level big enough that
importing it into the catalogue would be obvious.

Three of the four checks are about the pictures being right. The fourth is the one that would
otherwise go unnoticed for months: **the catalogue must contain no mesh datablocks.** Rendering
imports every GLB the prefabs reference, and if that geometry is still alive when ``prefabs.blend``
is written, the catalogue silently grows from kilobytes to hundreds of megabytes and the empty --
whose entire purpose is that a drop does not append geometry -- stops meaning anything. Nothing
about that fails loudly at the time.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from paradise_assets import catalogue, thumbnail
from paradise_assets.document import project

DEFAULT_PROJECT = r"C:\proj\paradise-workspace\shiningpie"

failures: list[str] = []


def check(condition: bool, label: str) -> bool:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)
    return condition


def coverage(png: str) -> float:
    """Fraction of the image that is not transparent."""
    image = bpy.data.images.load(png)
    try:
        pixels = list(image.pixels)
        opaque = sum(1 for i in range(3, len(pixels), 4) if pixels[i] > 0.01)
        return opaque / max(len(pixels) // 4, 1)
    finally:
        bpy.data.images.remove(image)


# --------------------------------------------------------------------------------------
# Part 1 -- a real project
# --------------------------------------------------------------------------------------


def against_real_project(root: str) -> None:
    layout = project.locate(root)
    if layout is None:
        print(f"SKIP  no asset project at {root}")
        return

    made, pictured, warnings = catalogue.build(layout.root)
    for warning in warnings:
        print(f"      warning: {warning}")

    check(made > 0, f"the catalogue holds {made} prefab(s)")
    check(pictured > 0, f"{pictured} of {made} prefab(s) got a rendered thumbnail")

    # Every picture has to be a picture OF something. An empty frame and a camera buried inside
    # the geometry both render without erroring, and both look like a broken asset in the browser
    # -- the first as blank, the second as a wall of colour.
    previews = thumbnail.previews_directory(layout.root)
    pngs = [
        os.path.join(base, name)
        for base, _, names in os.walk(previews)
        for name in names
        if name.endswith(".png")
    ]
    check(len(pngs) == pictured, f"{len(pngs)} png(s) on disk for {pictured} thumbnail(s)")

    framed = {png: coverage(png) for png in pngs}
    bad = {p: c for p, c in framed.items() if not 0.005 < c < 0.95}
    check(
        not bad,
        "every thumbnail is framed (0.5%-95% coverage)"
        + ("" if not bad else f" -- {[(os.path.basename(p), round(c, 3)) for p, c in bad.items()]}"),
    )

    # The previews survive the save and come back at full size. A clamp would read back as 128.
    bpy.ops.wm.open_mainfile(filepath=catalogue.catalogue_path(layout.root))
    assets = [o for o in bpy.data.objects if o.asset_data is not None]
    check(len(assets) == made, f"{len(assets)} asset(s) in the reloaded catalogue")

    sized = [
        o for o in assets
        if o.preview is not None and tuple(o.preview.image_size) == (thumbnail.SIZE, thumbnail.SIZE)
    ]
    check(
        len(sized) == pictured,
        f"{len(sized)} asset(s) carry a {thumbnail.SIZE}px preview after reload",
    )

    with_pixels = [o for o in sized if any(v > 0.0 for v in o.preview.image_pixels_float)]
    check(len(with_pixels) == len(sized), "every reloaded preview has non-blank pixels")

    # THE check. See the module docstring.
    check(
        len(bpy.data.meshes) == 0,
        f"the catalogue contains no mesh datablocks (found {len(bpy.data.meshes)})",
    )
    check(
        len(bpy.data.images) == 0,
        f"the catalogue contains no image datablocks (found {len(bpy.data.images)})",
    )

    size_mb = os.path.getsize(catalogue.catalogue_path(layout.root)) / (1024 * 1024)
    check(size_mb < 16.0, f"the catalogue is {size_mb:.2f} MB, not a copy of the project")


# --------------------------------------------------------------------------------------
# Part 2 -- incremental rebuilds, on a project we are allowed to modify
# --------------------------------------------------------------------------------------
#
# Synthetic rather than a copy of ShiningPie: the point is to touch a referenced GLB and watch
# exactly one thumbnail come back, which needs a project small enough that "exactly one" is a
# statement about the cache rather than about which 117 files happened to change.

PROJECT_TOML = 'schema_version = 1\nname = "thumbnail-cache-probe"\n'

PREFAB = """schema_version = 1

[[objects]]

[[objects.components]]
id = "0f1d4b3a-8c27-4a55-9b6e-2f7c1d40a913"
type = "meta"
Guid = "{guid}"
Name = "{name}"

[[objects.components]]
id = "7e55c210-3d41-4b8a-8f26-9c0a5e71b4d2"
type = "transform"
Position = [0.0, 0.0, 0.0]
Rotation = [0.0, 0.0, 0.0, 1.0]
Scale = [1.0, 1.0, 1.0]

[[objects.components]]
id = "edee8bd8-9321-47db-819d-9bdadf010be4"
type = "Probe.Mesh"
Mesh = {{ guid = "{mesh_guid}", path = "Models/{mesh}.glb" }}
"""

META = 'schema_version = 1\nguid = "{guid}"\nkind = "document"\n'


def write_glb(path: str, kind: str) -> None:
    """Export a primitive as a GLB the prefab can reference."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if kind == "cube":
        bpy.ops.mesh.primitive_cube_add(size=2.0)
    else:
        bpy.ops.mesh.primitive_uv_sphere_add(radius=1.0)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", use_selection=False)


def build_probe_project(root: str) -> None:
    assets = os.path.join(root, "assets")
    os.makedirs(os.path.join(assets, "prefabs"), exist_ok=True)
    with open(os.path.join(assets, "project.toml"), "w", encoding="utf-8") as handle:
        handle.write(PROJECT_TOML)

    for index, (name, mesh, kind) in enumerate(
        [("Boxy", "boxy", "cube"), ("Bally", "bally", "sphere")]
    ):
        write_glb(os.path.join(assets, "Models", f"{mesh}.glb"), kind)
        guid = f"11111111-2222-4333-8444-00000000000{index}"
        prefab = os.path.join(assets, "prefabs", f"{mesh}.prefab")
        with open(prefab, "w", encoding="utf-8") as handle:
            handle.write(PREFAB.format(guid=guid, name=name, mesh=mesh, mesh_guid=guid))
        with open(prefab + ".meta", "w", encoding="utf-8") as handle:
            handle.write(META.format(guid=guid))


def stamps(previews: str) -> dict[str, float]:
    return {
        name: os.path.getmtime(os.path.join(previews, "prefabs", name))
        for name in os.listdir(os.path.join(previews, "prefabs"))
        if name.endswith(".png")
    }


def incremental_rebuilds() -> None:
    with tempfile.TemporaryDirectory() as root:
        build_probe_project(root)
        previews = thumbnail.previews_directory(root)

        made, pictured, warnings = catalogue.build(root)
        for warning in warnings:
            print(f"      warning: {warning}")
        if not check(made == 2 and pictured == 2, f"probe project builds 2 prefabs, 2 thumbnails "
                                                  f"(got {made}, {pictured})"):
            return

        first = stamps(previews)
        check(
            set(first) == {"boxy.png", "bally.png"},
            f"thumbnails mirror the prefab paths under assets/ ({sorted(first)})",
        )

        # The manifest is what the second build reads. It must name the prefab AND the GLB --
        # reported by the loader, not guessed at -- or the cache is keyed on half the truth.
        with open(os.path.join(previews, "prefabs", "boxy.json"), encoding="utf-8") as handle:
            sources = json.load(handle)["sources"]
        named = {os.path.basename(p) for p in sources}
        check(
            {"boxy.prefab", "boxy.glb"} <= named,
            f"the manifest records the prefab and its mesh ({sorted(named)})",
        )

        # mtime resolution is coarse enough on some filesystems that an immediate rewrite can
        # land in the same tick as the render it should invalidate.
        time.sleep(1.1)

        catalogue.build(root)
        check(stamps(previews) == first, "an unchanged project re-renders nothing")

        # Touch ONE referenced GLB. Its thumbnail must come back; the other must not.
        write_glb(os.path.join(root, "assets", "Models", "boxy.glb"), "cube")
        catalogue.build(root)
        second = stamps(previews)
        check(
            second["boxy.png"] > first["boxy.png"],
            "a re-exported GLB re-renders the prefab that references it",
        )
        check(
            second["bally.png"] == first["bally.png"],
            "and leaves every other prefab's thumbnail alone",
        )


def main() -> int:
    root = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else DEFAULT_PROJECT

    print("== a real project ==")
    against_real_project(root)
    print("\n== incremental rebuilds ==")
    incremental_rebuilds()

    print(f"\n{len(failures)} failure(s)")
    for label in failures:
        print(f"  {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
