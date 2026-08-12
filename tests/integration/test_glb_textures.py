"""Pin the GLB→KTX2-sidecar externalization the engine's texture contract depends on.

The engine reads textured meshes via external image URIs resolved to ``.ktx2`` sidecars next to
the GLB, and rejects PNG/JPEG. Blender can only embed PNGs, so the exporter post-processes each
GLB (`pipeline/glb_textures.py`). The failure modes are all silent at export time — a broken
bufferView renumbering, a lost accessor, an sRGB normal map — and only surface as runtime
corruption, which is why this exercises the full path inside Blender with a real transcode.

Run with::

    blender --background --factory-startup --python tests/integration/test_glb_textures.py

Exits non-zero on failure so CI can gate on it. Skips (exit 0, loudly) without KTX-Software.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

import bpy  # noqa: E402

import paradise_blender  # noqa: E402
from paradise_blender.export.mesh import MeshExporter  # noqa: E402
from paradise_blender.paths import ExportPaths  # noqa: E402
from paradise_blender.pipeline import ktx  # noqa: E402


def build_textured_cube() -> bpy.types.Object:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.active_object
    cube.data.name = "TexturedCube"

    material = bpy.data.materials.new("TestMaterial")
    material.use_nodes = True
    bsdf = material.node_tree.nodes["Principled BSDF"]

    def image_node(name: str, color) -> bpy.types.Node:
        image = bpy.data.images.new(name, 8, 8)
        image.pixels[:] = list(color) * 64
        image.pack()
        node = material.node_tree.nodes.new("ShaderNodeTexImage")
        node.image = image
        return node

    base = image_node("T_Test_BaseColor", (0.8, 0.4, 0.1, 1.0))
    material.node_tree.links.new(bsdf.inputs["Base Color"], base.outputs["Color"])

    # A second image on the normal socket, NAMED like a normal map: the sidecar must come out
    # LINEAR (ktx.is_linear keys on the name), or lighting silently warps.
    normal_image = image_node("T_Test_Normal", (0.5, 0.5, 1.0, 1.0))
    normal_image.image.colorspace_settings.name = "Non-Color"
    normal_map = material.node_tree.nodes.new("ShaderNodeNormalMap")
    material.node_tree.links.new(normal_map.inputs["Color"], normal_image.outputs["Color"])
    material.node_tree.links.new(bsdf.inputs["Normal"], normal_map.outputs["Normal"])

    cube.data.materials.append(material)
    return cube


def main() -> int:
    if ktx.resolve_transcoder() is None:
        print("SKIPPED: KTX-Software not found; sidecar externalization not exercised.")
        return 0

    failures: list[str] = []

    def check(label: str, condition: bool) -> None:
        if condition:
            print(f"ok   {label}")
        else:
            failures.append(label)

    cube = build_textured_cube()
    paradise_blender.register()
    data_dir = os.path.join(tempfile.mkdtemp(prefix="paradise_glbtex_test"), "data")
    paths = ExportPaths(data_dir)
    paths.ensure_output_directory()

    field = MeshExporter().resolve_mesh_field(cube, paths)
    check("mesh exported", field is not None)
    glb_path = paths.output_path_for_field(field)

    with open(glb_path, "rb") as handle:
        blob = handle.read()
    json_length, _ = struct.unpack_from("<II", blob, 12)
    document = json.loads(blob[20 : 20 + json_length])

    images = document.get("images") or []
    check("both images externalized", len(images) == 2)
    for image in images:
        check(f"image '{image.get('name')}' has a .ktx2 uri", str(image.get("uri", "")).endswith(".ktx2"))
        check(f"image '{image.get('name')}' has no bufferView", "bufferView" not in image)
        sidecar = os.path.join(os.path.dirname(glb_path), image["uri"])
        exists = os.path.exists(sidecar)
        check(f"sidecar '{image.get('uri')}' exists", exists)
        if exists:
            with open(sidecar, "rb") as handle:
                magic = handle.read(12)
            check(f"sidecar '{image.get('uri')}' is KTX2", magic == b"\xabKTX 20\xbb\r\n\x1a\n")

    check("no PNG bytes remain in the GLB", b"\x89PNG" not in blob)

    # The compaction must leave every accessor's bufferView pointing at a real entry with the
    # bytes it claims — the engine hard-validates this, and an off-by-one here renders garbage.
    views = document.get("bufferViews") or []
    buffer_length = document["buffers"][0]["byteLength"]
    for index, accessor in enumerate(document.get("accessors") or []):
        view = views[accessor["bufferView"]]
        check(
            f"accessor {index} inside the compacted buffer",
            view.get("byteOffset", 0) + view["byteLength"] <= buffer_length,
        )

    # Round-trip through Blender's own importer: it validates chunk structure, bufferView
    # bounds, and accessor consistency far more thoroughly than any hand-rolled check.
    bpy.ops.wm.read_factory_settings(use_empty=True)
    try:
        bpy.ops.import_scene.gltf(filepath=glb_path)
        reimported = any(o.type == "MESH" for o in bpy.data.objects)
        check("Blender re-imports the compacted GLB", reimported)
    except Exception as error:
        failures.append(f"Blender re-import failed: {error}")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nGLB texture externalization passed.")
    return 0


if __name__ == "__main__":
    # Blender exits 0 when a --python script raises, so an unexpected crash would read as a
    # PASS in CI. Catch everything and exit non-zero ourselves.
    try:
        sys.exit(main())
    except Exception as error:
        import traceback

        traceback.print_exc()
        print(f"\nCRASHED: {error}")
        sys.exit(1)
