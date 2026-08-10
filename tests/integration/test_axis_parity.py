"""Pin the axis conversion against Blender's own glTF exporter.

This is the single most important test in the repository.

``RenderableComponentData.Mesh`` points at a GLB written by Blender's glTF exporter with
``export_yup=True``. The entity's ``WorldMatrix`` -- written by our
:mod:`paradise_blender.contract.axes` -- is what places that GLB in the world. If the two
disagree about what "Y-up" means, every mesh in every scene lands rotated 90 degrees about X,
and nothing else in the test suite would catch it: our own unit tests would still pass,
because they only check our conversion against itself.

So this test asks Blender to export a deliberately awkward object (rotated on all three axes,
non-uniformly scaled, translated), reads the node transform out of the resulting GLB, and
asserts it equals ``axes.convert_matrix(object.matrix_world)``.

Run with::

    blender --background --factory-startup --python tests/integration/test_axis_parity.py

Exits non-zero on failure so CI can gate on it.
"""

from __future__ import annotations

import json
import math
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import bpy
from mathutils import Euler, Matrix, Vector

from paradise_blender.contract import axes

TOLERANCE = 1e-5


def read_glb_gltf_chunk(path: str) -> dict:
    """Extract the JSON chunk from a GLB container.

    GLB layout: a 12-byte header (magic, version, length), then chunks of
    ``[u32 length][u32 type][payload]``. The first chunk is always the glTF JSON.
    """
    with open(path, "rb") as handle:
        magic, _version, _length = struct.unpack("<4sII", handle.read(12))
        if magic != b"glTF":
            raise ValueError(f"{path} is not a GLB container")

        chunk_length, chunk_type = struct.unpack("<II", handle.read(8))
        if chunk_type != 0x4E4F534A:  # 'JSON'
            raise ValueError("first GLB chunk is not JSON")

        return json.loads(handle.read(chunk_length))


def gltf_node_matrix(node: dict) -> Matrix:
    """Reconstruct a glTF node's local matrix from whichever form it uses.

    glTF allows either an explicit ``matrix`` (column-major, 16 floats) or a decomposed
    TRS triple; the exporter picks the shorter representation per node, so both must be handled.
    """
    if "matrix" in node:
        flat = node["matrix"]
        # Column-major -> mathutils' row-major indexing.
        return Matrix([[flat[column * 4 + row] for column in range(4)] for row in range(4)])

    translation = Vector(node.get("translation", (0.0, 0.0, 0.0)))
    rotation = node.get("rotation", (0.0, 0.0, 0.0, 1.0))  # glTF is (x, y, z, w)
    scale = Vector(node.get("scale", (1.0, 1.0, 1.0)))

    from mathutils import Quaternion

    quaternion = Quaternion((rotation[3], rotation[0], rotation[1], rotation[2]))
    return Matrix.LocRotScale(translation, quaternion, scale)


def matrices_close(a: Matrix, b: Matrix, tolerance: float = TOLERANCE) -> bool:
    return all(
        abs(a[row][column] - b[row][column]) <= tolerance
        for row in range(4)
        for column in range(4)
    )


def format_matrix(m) -> str:
    rows = []
    for row in range(4):
        rows.append("  [" + ", ".join(f"{m[row][column]:9.5f}" for column in range(4)) + "]")
    return "\n".join(rows)


def run_case(name: str, location, rotation_euler, scale) -> bool:
    """Export one transformed object and compare the GLB node transform to our conversion."""
    bpy.ops.wm.read_factory_settings(use_empty=True)

    bpy.ops.mesh.primitive_cube_add(size=1.0)
    obj = bpy.context.active_object
    obj.name = "Probe"
    obj.location = location
    obj.rotation_euler = Euler(rotation_euler, "XYZ")
    obj.scale = scale
    bpy.context.view_layer.update()

    expected = Matrix(axes.convert_matrix(axes.from_mathutils(obj.matrix_world)))

    output = os.path.join(tempfile.gettempdir(), f"paradise_axis_probe_{name}.glb")
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.export_scene.gltf(
        filepath=output,
        export_format="GLB",
        use_selection=True,
        export_yup=True,
        export_apply=False,
    )

    gltf = read_glb_gltf_chunk(output)
    nodes = [node for node in gltf.get("nodes", []) if "mesh" in node]
    if not nodes:
        print(f"FAIL [{name}]: the exported GLB has no mesh node")
        return False

    actual = gltf_node_matrix(nodes[0])

    if matrices_close(actual, expected):
        print(f"ok   [{name}]")
        return True

    print(f"FAIL [{name}]: our conversion disagrees with Blender's glTF exporter")
    print("  Blender matrix_world:")
    print(format_matrix(obj.matrix_world))
    print("  ours (contract/axes.convert_matrix):")
    print(format_matrix(expected))
    print("  glTF exporter (export_yup=True):")
    print(format_matrix(actual))
    return False


def main() -> int:
    cases = [
        ("identity", (0, 0, 0), (0, 0, 0), (1, 1, 1)),
        ("translate", (1, 2, 3), (0, 0, 0), (1, 1, 1)),
        # Yaw about Blender's up axis: must become yaw about the contract's up axis.
        ("yaw", (0, 0, 0), (0, 0, math.radians(90)), (1, 1, 1)),
        ("pitch", (0, 0, 0), (math.radians(45), 0, 0), (1, 1, 1)),
        # Non-uniform scale is where a naive per-component conversion breaks: Blender's
        # (1, 2, 3) is the contract's (1, 3, 2), and only converting the matrix gets that right.
        ("nonuniform_scale", (0, 0, 0), (0, 0, 0), (1, 2, 3)),
        (
            "combined",
            (1.5, -2.25, 3.75),
            (math.radians(30), math.radians(-45), math.radians(60)),
            (2, 0.5, 1.25),
        ),
    ]

    print("Axis parity: paradise_blender.contract.axes vs. Blender's glTF exporter\n")
    failures = [name for name, *args in cases if not run_case(name, *args)]

    print()
    if failures:
        print(f"{len(failures)} of {len(cases)} cases FAILED: {', '.join(failures)}")
        return 1

    print(f"All {len(cases)} cases passed — mesh data and node transforms agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
