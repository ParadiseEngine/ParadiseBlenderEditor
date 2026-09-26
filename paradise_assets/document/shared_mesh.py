"""Editing a SHARED model in place: the geometry Blender edits goes back into the model's own GLB.

An owned mesh (``editable_mesh.py``) is a copy, so its GLB can be whatever Blender's exporter
writes. A shared model is an artist's file that every placement, in every document, shows -- and
the exporter would flatten its node hierarchy into one mesh and replace its materials with
placeholders, taking the textures and every extracted material's source with them. So the save
exports the edited geometry to a scratch GLB and :func:`splice` writes a new model from the
ORIGINAL's JSON with only the geometry replaced: materials, textures, images (embedded bytes
included), samplers, nodes, names, extras and extensions stay exactly as they were.

**Slot i is primitive i**, in :func:`editable_mesh.mesh_instances` order, both ways:
``build_mesh`` baked every mesh node by its world transform into one mesh with a slot per
primitive, and the exporter writes one primitive per slot in slot order. :func:`splice` walks the
original the same way and gives each mesh node its slots back, moved into the node's own space
by the inverse of that world transform, each keeping its original ``material``. A mesh two nodes
shared becomes one mesh per node, since each can now be edited apart.

:func:`unsupported` refuses what that round trip would lose rather than drop it silently: morph
targets, vertex colours and second UV sets (``build_mesh`` reads none of them), a mesh node the
default scene does not reach, accessors or buffer views something other than a primitive or an
image uses (GPU instancing, say), and geometry that is not in the GLB's own binary chunk.

Imports no ``bpy``; pure Python, so the unit tests run it without Blender or numpy.
"""

from __future__ import annotations

import json
import math
import struct

from . import editable_mesh

__all__ = ["splice", "unsupported"]

#: The attributes ``build_mesh`` rebuilds; anything else on a primitive would be lost.
_EDITABLE_ATTRIBUTES = frozenset({"POSITION", "NORMAL", "TEXCOORD_0", "TANGENT"})

_FLOAT = 5126
_UINT = 5125
_ARRAY_BUFFER = 34962
_ELEMENT_ARRAY_BUFFER = 34963
_FORMATS = {5120: "b", 5121: "B", 5122: "h", 5123: "H", 5125: "I", 5126: "f"}
_WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}

_JSON_CHUNK = b"JSON"
_BIN_CHUNK = b"BIN\x00"

#: Primitive keys :func:`splice` rebuilds; every other key (``material``, ``extras``,
#: ``extensions``) is carried over from the original primitive.
_REBUILT = frozenset({"attributes", "indices", "mode", "targets"})


def unsupported(document: dict) -> str | None:
    """Why the shared model ``document`` cannot be edited in place, or ``None``."""
    problem = editable_mesh.unsupported(document)
    if problem is not None:
        return problem
    if editable_mesh.owner_of_document(document) is not None:
        return "it is the mesh of one object; edit it on that object"

    buffers = document.get("buffers") or []
    if len(buffers) != 1 or not isinstance(buffers[0], dict) or "uri" in buffers[0]:
        return "its data is not all in the GLB's own binary chunk"

    instances = editable_mesh.mesh_instances(document)
    reached = {node for node, _mesh, _world in instances}
    nodes = document.get("nodes") or []
    for index, node in enumerate(nodes):
        if isinstance(node, dict) and "mesh" in node and index not in reached:
            return f"node {index} holds a mesh the default scene does not show"

    meshes = document["meshes"]
    geometry: set = set()
    for _node, mesh, world in instances:
        if abs(_determinant(world)) < 1e-12:
            return "a mesh node is scaled to nothing, so its geometry cannot be put back"
        for primitive in meshes[mesh]["primitives"]:
            if primitive.get("targets"):
                return "it has morph targets, which would be lost"
            extra = sorted(set(primitive["attributes"]) - _EDITABLE_ATTRIBUTES)
            if extra:
                return f"it has {', '.join(extra)}, which would be lost"
            geometry |= set(primitive["attributes"].values())
            geometry.add(primitive.get("indices"))
    accessors = document.get("accessors") or []
    if set(range(len(accessors))) - geometry:
        return "it keeps data other than its geometry in accessors, which would be lost"
    used_views = {item.get("bufferView") for item in accessors + (document.get("images") or [])
                  if isinstance(item, dict)}
    if set(range(len(document.get("bufferViews") or []))) - used_views:
        return "it keeps data the rewrite would not carry over in its binary chunk"
    return None


def splice(original: tuple[dict, bytes], exported: tuple[dict, bytes]) -> bytes:
    """A GLB that is ``original`` with its geometry replaced by ``exported``'s primitives.

    ``original`` is the shared model as ``gltf.read_glb`` returns it and must pass
    :func:`unsupported`. ``exported`` is Blender's export of the edited mesh: one mesh, in the
    model's space, a primitive per slot in slot order. Raises ``ValueError`` when the export does
    not have exactly one primitive per slot of the original.
    """
    document, binary = original
    fresh, fresh_binary = exported
    slots = _slots(document)
    primitives = _exported_primitives(fresh)
    if len(primitives) != len(slots):
        raise ValueError(f"the export has {len(primitives)} part(s) for {len(slots)} material slot(s)")

    out = json.loads(json.dumps(document))   # a deep copy of plain JSON
    writer = _Writer()
    views: list[dict] = []
    view_of: dict[int, int] = {}
    for image in out.get("images") or []:
        if isinstance(image, dict) and isinstance(image.get("bufferView"), int):
            old = image["bufferView"]
            if old not in view_of:
                view_of[old] = len(views)
                views.append(writer.copy_view(document["bufferViews"][old], binary))
            image["bufferView"] = view_of[old]

    reader = _Reader(fresh, fresh_binary)
    accessors: list[dict] = []
    meshes: list[dict] = []
    nodes = out["nodes"]
    at = 0
    for node, mesh_index, world in editable_mesh.mesh_instances(document):
        source = document["meshes"][mesh_index]
        placed = _Placement(world)
        mesh = {key: value for key, value in source.items() if key not in ("primitives", "weights")}
        mesh["primitives"] = []
        for original_primitive in source["primitives"]:
            edited = primitives[at]
            at += 1
            primitive = {key: value for key, value in original_primitive.items() if key not in _REBUILT}
            primitive["attributes"] = {
                name: writer.accessor(reader, index, name, placed, accessors, views)
                for name, index in edited["attributes"].items()
            }
            primitive["indices"] = writer.indices(reader, edited, placed, accessors, views)
            mesh["primitives"].append(primitive)
        nodes[node]["mesh"] = len(meshes)
        meshes.append(mesh)

    out["meshes"] = meshes
    out["accessors"] = accessors
    out["bufferViews"] = views
    out["buffers"] = [{"byteLength": len(writer.data)}]
    return _container(out, bytes(writer.data))


def _slots(document: dict) -> list[tuple[int, int]]:
    """``(mesh index, primitive index)`` per slot, in slot order."""
    return [
        (mesh, index)
        for _node, mesh, _world in editable_mesh.mesh_instances(document)
        for index in range(len(document["meshes"][mesh]["primitives"]))
    ]


def _exported_primitives(document: dict) -> list[dict]:
    meshes = document.get("meshes") or []
    if len(meshes) != 1:
        raise ValueError(f"the export holds {len(meshes)} meshes, not one")
    return meshes[0].get("primitives") or []


# -- the transform back into a node's own space ---------------------------------------------------

def _determinant(world: tuple[float, ...]) -> float:
    m = world
    return (m[0] * (m[5] * m[10] - m[9] * m[6]) - m[4] * (m[1] * m[10] - m[9] * m[2])
            + m[8] * (m[1] * m[6] - m[5] * m[2]))


class _Placement:
    """Undoes ``build_mesh``'s bake of one mesh node: world ``W`` took local ``p`` to ``W p``,
    so the local point is ``W^-1 p``, a normal ``normalize(L^T n)`` and a tangent
    ``normalize(L^-1 t)`` (``L`` the linear part). A mirror (negative determinant) had its
    winding flipped by the bake, so it is flipped back, and the tangent's handedness with it."""

    def __init__(self, world: tuple[float, ...]) -> None:
        m = world   # column-major: m[column * 4 + row]
        self.identity = all(abs(a - b) < 1e-12 for a, b in zip(m, editable_mesh.IDENTITY, strict=True))
        determinant = _determinant(m)
        self.mirrored = determinant < 0
        linear = [[m[column * 4 + row] for column in range(3)] for row in range(3)]
        self.linear = linear
        self.inverse = _inverse3(linear, determinant)
        self.translation = (m[12], m[13], m[14])

    def position(self, value):
        x, y, z = (value[i] - self.translation[i] for i in range(3))
        return tuple(r[0] * x + r[1] * y + r[2] * z for r in self.inverse)

    def normal(self, value):
        linear = self.linear
        return _normalized(tuple(
            linear[0][i] * value[0] + linear[1][i] * value[1] + linear[2][i] * value[2] for i in range(3)
        ))

    def tangent(self, value):
        direction = _normalized(tuple(r[0] * value[0] + r[1] * value[1] + r[2] * value[2]
                                      for r in self.inverse))
        return (*direction, -value[3] if self.mirrored else value[3])


def _inverse3(m, determinant):
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    return [
        [(e * i - f * h) / determinant, (c * h - b * i) / determinant, (b * f - c * e) / determinant],
        [(f * g - d * i) / determinant, (a * i - c * g) / determinant, (c * d - a * f) / determinant],
        [(d * h - e * g) / determinant, (b * g - a * h) / determinant, (a * e - b * d) / determinant],
    ]


def _normalized(vector):
    length = math.sqrt(sum(component * component for component in vector))
    return tuple(component / length for component in vector) if length > 0 else vector


# -- reading the export, writing the result -------------------------------------------------------

class _Reader:
    """Rows of the export's accessors -- the few shapes Blender's exporter writes, strided or not."""

    def __init__(self, document: dict, binary: bytes) -> None:
        self._accessors = document.get("accessors") or []
        self._views = document.get("bufferViews") or []
        self._binary = binary

    def rows(self, index: int) -> tuple[dict, list[tuple]]:
        accessor = self._accessors[index]
        if "sparse" in accessor or accessor.get("componentType") not in _FORMATS:
            raise ValueError("the export stores geometry in a form this writer does not take")
        view = self._views[accessor["bufferView"]]
        width = _WIDTHS[accessor["type"]]
        row = struct.Struct("<" + _FORMATS[accessor["componentType"]] * width)
        stride = view.get("byteStride") or row.size
        start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
        count = accessor["count"]
        if count and start + stride * (count - 1) + row.size > len(self._binary):
            raise ValueError("the export's geometry runs past its binary chunk")
        return accessor, [row.unpack_from(self._binary, start + stride * n) for n in range(count)]


class _Writer:
    """The new binary chunk, one tightly packed, 4-byte aligned view per accessor."""

    def __init__(self) -> None:
        self.data = bytearray()

    def _view(self, payload: bytes, target: int | None) -> dict:
        self.data += b"\x00" * (-len(self.data) % 4)
        view = {"buffer": 0, "byteOffset": len(self.data), "byteLength": len(payload)}
        if target is not None:
            view["target"] = target
        self.data += payload
        return view

    def copy_view(self, view: dict, binary: bytes) -> dict:
        start = view.get("byteOffset", 0)
        copied = self._view(binary[start:start + view["byteLength"]], None)
        for key in ("name", "extras", "extensions"):
            if key in view:
                copied[key] = view[key]
        return copied

    def accessor(self, reader: _Reader, index: int, name: str, placed: _Placement, accessors, views) -> int:
        accessor, rows = reader.rows(index)
        component = accessor["componentType"]
        if not placed.identity and name in ("POSITION", "NORMAL", "TANGENT"):
            if component != _FLOAT:
                raise ValueError(f"the export's {name} is not float")
            rows = [getattr(placed, name.lower())(row) for row in rows]
        width = _WIDTHS[accessor["type"]]
        payload = struct.pack(f"<{len(rows) * width}{_FORMATS[component]}",
                              *(value for row in rows for value in row))
        views.append(self._view(payload, _ARRAY_BUFFER))
        written = {"bufferView": len(views) - 1, "componentType": component,
                   "count": len(rows), "type": accessor["type"]}
        if accessor.get("normalized"):
            written["normalized"] = True
        if name == "POSITION" and rows:   # required by glTF, and moved by the transform
            written["min"] = [min(row[i] for row in rows) for i in range(3)]
            written["max"] = [max(row[i] for row in rows) for i in range(3)]
        accessors.append(written)
        return len(accessors) - 1

    def indices(self, reader: _Reader, primitive: dict, placed: _Placement, accessors, views) -> int:
        if "indices" in primitive:
            _accessor, rows = reader.rows(primitive["indices"])
            flat = [row[0] for row in rows]
        else:
            flat = list(range(reader.rows(primitive["attributes"]["POSITION"])[0]["count"]))
        if placed.mirrored:
            for start in range(0, len(flat) - 2, 3):
                flat[start], flat[start + 2] = flat[start + 2], flat[start]   # build_mesh's [::-1]
        views.append(self._view(struct.pack(f"<{len(flat)}I", *flat), _ELEMENT_ARRAY_BUFFER))
        accessors.append({"bufferView": len(views) - 1, "componentType": _UINT,
                          "count": len(flat), "type": "SCALAR"})
        return len(accessors) - 1


def _container(document: dict, binary: bytes) -> bytes:
    payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
    payload += b" " * (-len(payload) % 4)
    chunks = struct.pack("<I4s", len(payload), _JSON_CHUNK) + payload
    binary += b"\x00" * (-len(binary) % 4)
    chunks += struct.pack("<I4s", len(binary), _BIN_CHUNK) + binary
    return struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)) + chunks
