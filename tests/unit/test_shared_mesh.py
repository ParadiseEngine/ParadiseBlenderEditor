"""Writing an edit back into a SHARED model: the geometry changes, nothing else of the file does.
The Blender half (building, exporting) is ``tests/integration``'s.
"""

from __future__ import annotations

import json
import struct

import pytest

from paradise_assets.document import editable_mesh, gltf, shared_mesh

IMAGE = b"\x89PNG-not-really-but-bytes"


class _Builder:
    """A GLB's JSON and binary chunk, one tightly packed float/uint view per accessor."""

    def __init__(self) -> None:
        self.document: dict = {"asset": {"version": "2.0"}, "accessors": [], "bufferViews": []}
        self.binary = bytearray()

    def _view(self, payload: bytes) -> int:
        self.binary += b"\x00" * (-len(self.binary) % 4)
        self.document["bufferViews"].append(
            {"buffer": 0, "byteOffset": len(self.binary), "byteLength": len(payload)})
        self.binary += payload
        return len(self.document["bufferViews"]) - 1

    def floats(self, rows, kind: str) -> int:
        flat = [value for row in rows for value in row]
        view = self._view(struct.pack(f"<{len(flat)}f", *flat))
        self.document["accessors"].append(
            {"bufferView": view, "componentType": 5126, "count": len(rows), "type": kind})
        return len(self.document["accessors"]) - 1

    def indices(self, values) -> int:
        view = self._view(struct.pack(f"<{len(values)}H", *values))
        self.document["accessors"].append(
            {"bufferView": view, "componentType": 5123, "count": len(values), "type": "SCALAR"})
        return len(self.document["accessors"]) - 1

    def image(self, payload: bytes) -> int:
        return self._view(payload)

    def primitive(self, points, material=None) -> dict:
        primitive = {
            "attributes": {
                "POSITION": self.floats(points, "VEC3"),
                "NORMAL": self.floats([(0.0, 0.0, 1.0)] * len(points), "VEC3"),
                "TANGENT": self.floats([(1.0, 0.0, 0.0, 1.0)] * len(points), "VEC4"),
            },
            "indices": self.indices(list(range(len(points)))),
        }
        if material is not None:
            primitive["material"] = material
        return primitive

    def done(self) -> tuple[dict, bytes]:
        self.document["buffers"] = [{"byteLength": len(self.binary)}]
        return self.document, bytes(self.binary)


TRIANGLE = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]


def shared_model() -> tuple[dict, bytes]:
    """Two nodes: a parent moved by +10 X holding two materials, and a child mirrored in X."""
    builder = _Builder()
    image = builder.image(IMAGE)
    builder.document.update({
        "scene": 0,
        "scenes": [{"nodes": [0], "extras": {"author": "artist"}}],
        "nodes": [
            {"name": "Body", "mesh": 0, "translation": [10, 0, 0], "children": [1]},
            {"name": "Wing", "mesh": 1, "scale": [-1, 1, 1]},
        ],
        "meshes": [
            {"name": "BodyMesh",
             "primitives": [builder.primitive(TRIANGLE, 0), builder.primitive(TRIANGLE, 1)]},
            {"name": "WingMesh", "primitives": [builder.primitive(TRIANGLE, 1)]},
        ],
        "materials": [{"name": "Paint", "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}},
                      {"name": "Chrome", "extras": {"keep": True}}],
        "textures": [{"source": 0, "sampler": 0}],
        "samplers": [{"magFilter": 9729}],
        "images": [{"bufferView": image, "mimeType": "image/png"}],
    })
    return builder.done()


def export_of(parts) -> tuple[dict, bytes]:
    """What Blender's exporter writes for the edited mesh: one mesh, a primitive per slot, in the
    model's space."""
    builder = _Builder()
    builder.document.update({
        "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [builder.primitive(points) for points in parts]}],
    })
    return builder.done()


def read_rows(document: dict, binary: bytes, index: int) -> list[tuple]:
    accessor = document["accessors"][index]
    view = document["bufferViews"][accessor["bufferView"]]
    width = {"SCALAR": 1, "VEC3": 3, "VEC4": 4}[accessor["type"]]
    code = {5126: "f", 5125: "I", 5123: "H"}[accessor["componentType"]]
    row = struct.Struct("<" + code * width)
    return [row.unpack_from(binary, view["byteOffset"] + n * row.size) for n in range(accessor["count"])]


def world_points(document: dict, binary: bytes) -> list[list[tuple]]:
    """Every slot's triangle corners in model space, in slot order -- what ``build_mesh`` bakes,
    including its winding flip under a mirrored node."""
    slots = []
    for _node, mesh, world in editable_mesh.mesh_instances(document):
        mirrored = shared_mesh._determinant(world) < 0
        for primitive in document["meshes"][mesh]["primitives"]:
            points = read_rows(document, binary, primitive["attributes"]["POSITION"])
            order = [i[0] for i in read_rows(document, binary, primitive["indices"])]
            if mirrored:
                order = [corner for start in range(0, len(order), 3)
                         for corner in reversed(order[start:start + 3])]
            placed = [tuple(sum(world[c * 4 + r] * (points[i] + (1.0,))[c] for c in range(4))
                            for r in range(3))
                      for i in order]
            slots.append(placed)
    return slots


def rounded(slots):
    return [[tuple(round(value, 5) for value in point) for point in slot] for slot in slots]


def spliced(original, parts) -> tuple[dict, bytes]:
    path_bytes = shared_mesh.splice(original, export_of(parts))
    return _parse(path_bytes)


def _parse(data: bytes) -> tuple[dict, bytes]:
    length = struct.unpack_from("<I", data, 12)[0]
    document = json.loads(data[20:20 + length])
    binary_length = struct.unpack_from("<I", data, 20 + length)[0]
    return document, data[28 + length:28 + length + binary_length]


EDITED = [
    [(10.0, 0.0, 0.0), (12.0, 0.0, 0.0), (10.0, 3.0, 0.0)],
    [(10.0, 0.0, 1.0), (11.0, 0.0, 1.0), (10.0, 1.0, 1.0),
     (11.0, 1.0, 1.0), (12.0, 1.0, 1.0), (11.0, 2.0, 1.0)],
    [(9.0, 0.0, 0.0), (8.0, 0.0, 0.0), (9.0, 1.0, 0.0)],
]


class TestSplice:
    def test_every_slot_lands_where_it_was_edited_through_its_nodes_transform(self):
        document, binary = spliced(shared_model(), EDITED)

        # A mirrored node's winding is flipped back on the way in, so the corners of its
        # triangle come out in the order the bake gives them -- the order the edit wrote.
        assert rounded(world_points(document, binary)) == rounded(EDITED)

    def test_a_slot_can_change_its_triangle_count(self):
        document, _binary = spliced(shared_model(), EDITED)

        assert document["accessors"][document["meshes"][0]["primitives"][1]["indices"]]["count"] == 6

    def test_materials_textures_images_nodes_and_extras_are_kept(self):
        original = shared_model()
        document, binary = spliced(original, EDITED)

        for key in ("materials", "textures", "samplers", "scenes", "scene"):
            assert document[key] == original[0][key], key
        assert [node["name"] for node in document["nodes"]] == ["Body", "Wing"]
        assert [m["name"] for m in document["meshes"]] == ["BodyMesh", "WingMesh"]
        assert [p["material"] for m in document["meshes"] for p in m["primitives"]] == [0, 1, 1]
        view = document["bufferViews"][document["images"][0]["bufferView"]]
        assert binary[view["byteOffset"]:view["byteOffset"] + view["byteLength"]] == IMAGE

    def test_normals_and_tangents_are_carried_into_the_nodes_space(self):
        document, binary = spliced(shared_model(), EDITED)
        wing = document["meshes"][1]["primitives"][0]["attributes"]

        # The wing is mirrored in X: a +X tangent in model space is -X in its own, and the
        # handedness flips with the mirror.
        assert read_rows(document, binary, wing["TANGENT"])[0] == pytest.approx((-1.0, 0.0, 0.0, -1.0))
        assert read_rows(document, binary, wing["NORMAL"])[0] == pytest.approx((0.0, 0.0, 1.0))

    def test_bounds_are_the_edited_geometrys(self):
        document, _binary = spliced(shared_model(), EDITED)
        body = document["accessors"][document["meshes"][0]["primitives"][0]["attributes"]["POSITION"]]

        assert body["min"] == pytest.approx([0.0, 0.0, 0.0]) and body["max"] == pytest.approx([2.0, 3.0, 0.0])

    def test_a_mesh_two_nodes_shared_becomes_one_per_node(self):
        document, binary = shared_model()
        document["nodes"][1]["mesh"] = 0
        parts = [EDITED[0], EDITED[1], EDITED[2], EDITED[0]]

        out, _binary = spliced((document, binary), parts)

        assert [node["mesh"] for node in out["nodes"]] == [0, 1]
        assert len(out["meshes"]) == 2

    def test_an_export_that_lost_a_slot_is_refused(self):
        with pytest.raises(ValueError, match="2 part"):
            shared_mesh.splice(shared_model(), export_of(EDITED[:2]))

    def test_the_result_is_a_glb_the_reader_takes(self, tmp_path):
        path = tmp_path / "out.glb"
        path.write_bytes(shared_mesh.splice(shared_model(), export_of(EDITED)))

        document, _binary = gltf.read_glb(str(path))

        assert document["materials"][0]["name"] == "Paint"
        assert shared_mesh.unsupported(document) is None


class TestUnsupported:
    def test_a_static_model_is_editable_in_place(self):
        assert shared_mesh.unsupported(shared_model()[0]) is None

    @pytest.mark.parametrize(
        ("change", "words"),
        [
            (lambda d: _attributes(d).__setitem__("COLOR_0", 0), "COLOR_0"),
            (lambda d: _attributes(d).__setitem__("TEXCOORD_1", 0), "TEXCOORD_1"),
            (lambda d: d["meshes"][0]["primitives"][0].__setitem__("targets", [{"POSITION": 0}]), "morph"),
            (lambda d: d["nodes"].append({"mesh": 0}), "does not show"),
            (lambda d: d["buffers"][0].__setitem__("uri", "data.bin"), "binary chunk"),
            (lambda d: d["accessors"].append({"bufferView": 0, "count": 1, "type": "MAT4"}), "accessors"),
            (lambda d: d["bufferViews"].append({"buffer": 0, "byteLength": 4}), "binary chunk"),
            (lambda d: d["nodes"][0].__setitem__("scale", [0, 0, 0]), "scaled to nothing"),
            (lambda d: d["scenes"][0]["extras"].__setitem__(editable_mesh.OWNER_EXTRA, OWNER), "one object"),
            (lambda d: d.__setitem__("skins", [{}]), "rigged"),
        ],
    )
    def test_what_the_round_trip_would_lose_is_named(self, change, words):
        document = shared_model()[0]
        change(document)

        assert words in (shared_mesh.unsupported(document) or "")


OWNER = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _attributes(document: dict) -> dict:
    return document["meshes"][0]["primitives"][0]["attributes"]
