"""The Blender half of an editable mesh; ``document/editable_mesh.py`` holds the contract.

- :func:`build_mesh` turns a GLB into ONE Blender mesh with a material slot per glTF primitive,
  in the engine's slot order. Not Blender's importer, for two reasons found the hard way: it
  merges primitives that share a material (and a placeholder-material GLB is nothing BUT shared
  "no material"), and it names objects after nodes that ShiningPie's multi-part models reuse
  dozens of times -- so afterwards neither the slots nor the parts could be told apart.
- :func:`publish` is the save's half: export what changed, refuse what would bind materials to
  the wrong faces or overwrite someone else's GLB. A SHARED model (:func:`begin_shared`) is not
  exported as-is but spliced back into its own GLB (``document/shared_mesh.py``), so its
  materials, textures and node hierarchy survive, and every placement is re-imported after.
- :func:`stash`, :func:`materialize` and :func:`drop` are the load's half. An object whose GLB is
  byte-for-byte what it last wrote or read is KEPT across a reload, so quads, modifiers and
  everything else a GLB cannot hold survive for the author who made them; the GLB, triangulated
  and with modifiers applied, is what everyone else loads.
- Slot materials are DISPLAY only, one per ``Slots`` entry (``Paradise/<material document>``).
  The export writes placeholders, and the game binds ``Slots[i]`` to primitive ``i`` -- so the
  material a slot shows here is exactly what the game will draw there, and a reordered slot is
  visible before it is refused.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import replace

import bmesh
import bpy
import numpy as np
from mathutils.kdtree import KDTree

from ..document import editable_mesh as contract
from ..document import gltf, material_document, model_source, shared_mesh
from ..document import guid as document_guid
from ..document.project import ProjectLayout
from . import store
from .meshes import SOURCE_KEY, MeshLibrary, glb_of

__all__ = [
    "begin_shared",
    "build_mesh",
    "display_names",
    "drop",
    "fingerprint",
    "materialize",
    "publish",
    "refresh_display",
    "settle",
    "shared_editor",
    "shared_source",
    "stash",
    "write_initial",
]

#: Display materials are named ``Paradise/<material document path>``, shared by every slot and
#: object that binds that document.
DISPLAY_PREFIX = "Paradise/"
_NO_MATERIAL = "(no material)"
#: Blender's ID names hold 63 bytes; a longer path is shortened with a digest so two long paths
#: cannot collapse into one material.
_NAME_LIMIT = 63
_FALLBACK_COLOUR = (0.8, 0.8, 0.8, 1.0)
#: A temporary corner attribute carrying glTF normals through ``Mesh.validate``.
_CORNER_NORMALS = ".paradise_corner_normals"
#: Stitching rounds before giving up on T-junctions that keep appearing; real models need two.
_STITCH_PASSES = 8
#: The largest deviation, relative to an edge's length, that is still rounding rather than shape
#: (about 0.006 degrees).
_FLAT = 1e-4

_COMPONENT_TYPES = {5120: "<i1", 5121: "<u1", 5122: "<i2", 5123: "<u2", 5125: "<u4", 5126: "<f4"}
_NORMALIZED_MAX = {5120: 127.0, 5121: 255.0, 5122: 32767.0, 5123: 65535.0}
_WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}

#: ``foreach_get`` layout per attribute type, for the fingerprint. A type missing here is hashed
#: by name only: a change to it alone would not re-export, which is the right failure for data
#: the glTF exporter does not write either.
_ATTRIBUTES = {
    "FLOAT": ("value", 1, np.float32),
    "INT": ("value", 1, np.int32),
    "INT8": ("value", 1, np.int32),
    "BOOLEAN": ("value", 1, np.bool_),
    "FLOAT2": ("vector", 2, np.float32),
    "INT32_2D": ("value", 2, np.int32),
    "FLOAT_VECTOR": ("vector", 3, np.float32),
    "FLOAT_COLOR": ("color", 4, np.float32),
    "BYTE_COLOR": ("color", 4, np.float32),
    "QUATERNION": ("value", 4, np.float32),
}


# -- building a mesh from a GLB ----------------------------------------------------------------

def build_mesh(path: str, name: str) -> bpy.types.Mesh:
    """The GLB at ``path`` as one mesh in the model's own space: every mesh node baked by its
    world transform, one material slot per primitive in the engine's slot order.

    Raises only :class:`contract.EditableMeshError`: the file is untrusted, and every caller
    turns that one error into a refusal or the read-only fallback."""
    document, binary = gltf.read_glb(path)
    problem = contract.unsupported(document)
    if problem is not None:
        raise contract.EditableMeshError(f"{os.path.basename(path)} cannot be edited here: {problem}.")
    try:
        return _build_mesh(document, binary, path, name)
    except (TypeError, ValueError, KeyError, IndexError, AttributeError) as error:
        raise contract.EditableMeshError(
            f"{os.path.basename(path)} cannot be edited here: its geometry is malformed ({error})."
        ) from error


def _build_mesh(document: dict, binary: bytes, path: str, name: str) -> bpy.types.Mesh:
    accessors = _Accessors(document, binary)
    positions, normals, uvs, corners, slots = [], [], [], [], []
    has_normals = True
    base = 0
    for _node, mesh_index, world in contract.mesh_instances(document):
        matrix = np.array(world, dtype=np.float64).reshape(4, 4).T   # glTF stores column-major
        linear, translation = matrix[:3, :3], matrix[:3, 3]
        determinant = np.linalg.det(linear)
        normal_matrix = np.linalg.inv(linear).T if abs(determinant) > 1e-12 else linear
        for primitive in document["meshes"][mesh_index]["primitives"]:
            attributes = primitive["attributes"]
            position = accessors.read(attributes["POSITION"], 3)
            count = len(position)
            if "indices" in primitive:
                indices = accessors.read(primitive["indices"], 1).astype(np.int64).ravel()
            else:
                indices = np.arange(count, dtype=np.int64)
            if len(indices) % 3 or (len(indices) and (indices.min() < 0 or indices.max() >= count)):
                raise contract.EditableMeshError(f"{os.path.basename(path)} has a malformed primitive.")
            triangles = indices.reshape(-1, 3)
            if determinant < 0:
                # A mirrored node turns its triangles inside out; baked, they must be flipped back.
                triangles = triangles[:, ::-1]

            positions.append(position @ linear.T + translation)
            if "NORMAL" in attributes:
                normal = accessors.read(attributes["NORMAL"], 3) @ normal_matrix.T
                length = np.linalg.norm(normal, axis=1, keepdims=True)
                normals.append(np.divide(normal, length, out=np.zeros_like(normal), where=length > 0))
            else:
                has_normals = False
                normals.append(np.zeros((count, 3)))
            uvs.append(accessors.read(attributes["TEXCOORD_0"], 2) if "TEXCOORD_0" in attributes
                       else np.zeros((count, 2)))
            corners.append(triangles + base)
            slots.append(np.full(len(triangles), len(slots), dtype=np.int32))
            base += count

    return _mesh(name, positions, normals if has_normals else None, uvs, corners, slots)


def _mesh(name, positions, normals, uvs, corners, slots) -> bpy.types.Mesh:
    mesh = bpy.data.meshes.new(name)
    for _ in slots:
        mesh.materials.append(None)
    if not positions:
        return mesh

    # glTF is Y-up; Blender is Z-up: (x, y, z) -> (x, -z, y), the importer's own swizzle.
    points = _to_blender(np.concatenate(positions))
    rows = np.concatenate(corners).ravel()
    mesh.vertices.add(len(points))
    mesh.vertices.foreach_set("co", points.astype(np.float32).ravel())
    mesh.loops.add(rows.size)
    mesh.loops.foreach_set("vertex_index", rows.astype(np.int32))
    mesh.polygons.add(rows.size // 3)
    mesh.polygons.foreach_set("loop_start", np.arange(0, rows.size, 3, dtype=np.int32))
    mesh.polygons.foreach_set("material_index", np.concatenate(slots))

    texture = np.concatenate(uvs)
    texture[:, 1] = 1.0 - texture[:, 1]   # glTF's V runs down the image; Blender's runs up
    layer = mesh.uv_layers.new(name="UVMap")
    layer.uv.foreach_set("vector", texture[rows].astype(np.float32).ravel())
    if normals is not None:
        # Welded vertices keep their own corner normals -- per-vertex normals would smooth the
        # hard edges the split was for. Blender derives custom normals from final edges, so
        # they ride through the weld and validate() as a corner attribute and are set last.
        carried = mesh.attributes.new(_CORNER_NORMALS, "FLOAT_VECTOR", "CORNER")
        carried.data.foreach_set("vector", _to_blender(np.concatenate(normals))[rows].astype(np.float32).ravel())

    mesh.update(calc_edges=True)
    _weld(mesh, _weld_distance(points))
    mesh.validate(clean_customdata=False)
    if normals is not None:
        carried = mesh.attributes[_CORNER_NORMALS]
        values = np.empty(len(mesh.loops) * 3, dtype=np.float32)
        carried.data.foreach_get("vector", values)
        mesh.attributes.remove(carried)
        mesh.shade_smooth()
        mesh.normals_split_custom_set(values.reshape(-1, 3).tolist())
    return mesh


def _weld_distance(points: np.ndarray) -> float:
    """How far apart two rows may be and still be one corner. glTF has no per-corner vertex: any
    attribute difference splits a corner into its own row, and those rows are only NEARLY equal
    once exporters and node transforms have rounded them to float32 -- a few ulps of the model's
    largest coordinate. Detail finer than 10 µm is kept apart."""
    return max(1e-5, 8 * float(np.finfo(np.float32).eps) * float(np.abs(points).max(initial=0.0)))


def _weld(mesh: bpy.types.Mesh, distance: float) -> None:
    """Make the surface one piece to edit: merge vertices closer than ``distance``, then stitch
    T-junctions. Corner data (UVs, carried normals) and face data (material slots) stay with
    their faces."""
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=distance)
        _stitch(bm, distance)
        bm.to_mesh(mesh)
    finally:
        bm.free()


def _stitch(bm: bmesh.types.BMesh, distance: float) -> None:
    """Join every vertex lying inside another triangle's edge to that triangle. Parts modelled
    separately and joined meet a corner of one against an edge of the other (a T-junction): it
    renders watertight, but only one side owns the vertex, so dragging it opens a crack in the
    other. Each triangle with such vertices on its edges is replaced by triangles that use them,
    its corner data interpolated from the original.

    Slivers -- triangles flat to within :func:`_off_line` -- go first. Triangulating an n-gon
    with a vertex on a straight side leaves one, and it covers nothing: its long side is the
    T-junction the neighbour across it gets stitched along, and its short sides join what it
    separated.

    A new triangle's inner edge can cross another part's vertex in turn, so the new edges are
    searched again until none does: a rebuilt mesh then stitches to itself, unchanged."""
    slivers = []
    for face in bm.faces:
        longest = max(edge.calc_length() for edge in face.edges)
        if 2 * face.calc_area() <= _off_line(longest, distance) * longest:
            slivers.append(face)
    bmesh.ops.delete(bm, geom=slivers, context="FACES_ONLY")
    edges = list(bm.edges)
    for _ in range(_STITCH_PASSES):
        on_edge = _junctions(bm, edges, distance)
        if not on_edge:
            break
        replaced = {face for edge in on_edge for face in edge.link_faces if len(face.verts) == 3}
        made = [made for face in replaced for made in _split(bm, face, on_edge)]
        bmesh.ops.delete(bm, geom=list(replaced), context="FACES_ONLY")
        edges = list({edge for face in made if face.is_valid for edge in face.edges})
    bmesh.ops.delete(bm, geom=[edge for edge in bm.edges if not edge.link_faces], context="EDGES")
    bmesh.ops.delete(bm, geom=[vert for vert in bm.verts if not vert.link_faces], context="VERTS")


def _split(bm: bmesh.types.BMesh, face, on_edge: dict) -> list:
    """The triangles replacing ``face`` so that it uses every vertex ``on_edge`` puts on its sides."""
    corners = [loop.vert for loop in face.loops]
    sides = []
    for loop in face.loops:
        points = on_edge.get(loop.edge, [])
        sides.append(points if loop.edge.verts[0] is loop.vert else points[::-1])
    made = []
    for triangle in _fan(*corners, *sides):
        try:
            new = bm.faces.new(triangle, face)
        except ValueError:   # the same triangle already exists: overlapping geometry
            continue
        for loop in new.loops:
            loop.copy_from_face_interp(face)
        made.append(new)
    return made


def _off_line(length, distance: float):
    """How far a point may sit from a ``length``-long edge (or array of them) and still lie ON
    it: rounding, not shape. Within ``distance``, and within a sliver of the edge's length, so
    fine curved detail -- sub-millimetre edges bending by a few µm -- is not straightened."""
    return np.minimum(distance, np.multiply(length, _FLAT))


def _junctions(bm: bmesh.types.BMesh, edges, distance: float) -> dict:
    """Each of ``edges`` with vertices of other faces strictly inside it (within
    :func:`_off_line` of the segment, farther than ``distance`` from both ends): the vertices,
    ordered from ``edge.verts[0]``."""
    bm.verts.index_update()
    bm.verts.ensure_lookup_table()
    verts = bm.verts
    co = np.array([vert.co[:] for vert in verts], dtype=np.float64).reshape(-1, 3)
    tree = KDTree(len(verts))
    for index, point in enumerate(co.tolist()):
        tree.insert(point, index)
    tree.balance()

    found = {}
    for edge in edges:
        start, end = edge.verts
        axis = co[end.index] - co[start.index]
        length = float(np.linalg.norm(axis))
        if length <= 2 * distance:
            continue
        near = np.array([index for _, index, _ in tree.find_range(
            (start.co + end.co) / 2, length / 2 + distance)])
        offset = co[near] - co[start.index]
        along = offset @ axis / length
        apart = np.linalg.norm(offset - np.outer(along / length, axis), axis=1)
        inside = (along > distance) & (along < length - distance) & (apart <= _off_line(length, distance))
        if not inside.any():
            continue
        own = {vert.index for face in edge.link_faces for vert in face.verts}
        points = sorted((a, i) for a, i in zip(along[inside].tolist(), near[inside].tolist()) if i not in own)
        if points:
            found[edge] = [verts[i] for _, i in points]
    return found


def _fan(a, b, c, ab, bc, ca) -> list[tuple]:
    """Triangle ``a b c`` with the points ``ab``, ``bc``, ``ca`` inside its sides, as triangles
    of the same winding using every point. One side at a time is fanned from its opposite
    corner, which is never on that side's line, so no triangle is degenerate; the two outer
    triangles of the fan inherit the remaining sides' points."""
    if not ab:
        if bc:
            return _fan(b, c, a, bc, ca, ab)
        if ca:
            return _fan(c, a, b, ca, ab, bc)
        return [(a, b, c)]
    chain = [a, *ab, b]
    triangles = []
    for p, q in zip(chain, chain[1:]):
        triangles += _fan(p, q, c, [], bc if q is b else [], ca if p is a else [])
    return triangles


def _to_blender(points: np.ndarray) -> np.ndarray:
    return np.stack((points[:, 0], -points[:, 2], points[:, 1]), axis=1)


class _Accessors:
    """Typed reads of the BIN chunk: the subset a static mesh uses, bounds-checked, strided."""

    def __init__(self, document: dict, binary: bytes) -> None:
        self._accessors = document.get("accessors") or []
        self._views = document.get("bufferViews") or []
        self._binary = binary

    def read(self, index, width: int) -> np.ndarray:
        accessor = self._at(self._accessors, index)
        kind = _COMPONENT_TYPES.get(accessor.get("componentType"))
        if kind is None or _WIDTHS.get(accessor.get("type")) != width or "sparse" in accessor:
            raise self._malformed()
        view = self._at(self._views, accessor.get("bufferView"))
        if view.get("buffer", 0) != 0:
            raise self._malformed()

        count = self._int(accessor.get("count", 0))
        dtype = np.dtype(kind)
        element = dtype.itemsize * width
        stride = self._int(view.get("byteStride") or element)
        view_start = self._int(view.get("byteOffset", 0))
        start = view_start + self._int(accessor.get("byteOffset", 0))
        end = start + (stride * (count - 1) + element if count else 0)
        limit = min(len(self._binary), view_start + self._int(view.get("byteLength", 0)))
        if stride < element or end > limit:
            raise self._malformed()

        if stride == element:
            data = np.frombuffer(self._binary, dtype, count * width, start).reshape(count, width)
        else:
            raw = np.frombuffer(self._binary, np.uint8, end - start, start)
            rows = np.lib.stride_tricks.as_strided(raw, (count, element), (stride, 1))
            data = np.ascontiguousarray(rows).view(dtype).reshape(count, width)

        values = data.astype(np.float64)
        if accessor.get("normalized"):
            values = np.maximum(values / _NORMALIZED_MAX[accessor["componentType"]], -1.0)
        return values

    @staticmethod
    def _at(items: list, index) -> dict:
        if not isinstance(index, int) or not 0 <= index < len(items) or not isinstance(items[index], dict):
            raise _Accessors._malformed()
        return items[index]

    @staticmethod
    def _int(value) -> int:
        """A size or offset: a non-negative integer, or the file is refused. A string, a float
        or a negative offset would otherwise reach ``np.frombuffer`` as something else."""
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _Accessors._malformed()
        return value

    @staticmethod
    def _malformed() -> contract.EditableMeshError:
        return contract.EditableMeshError(
            "the model's geometry is stored in a form this reader does not take")


# -- display materials ---------------------------------------------------------------------------

def display_names(payloads, count: int) -> list[str]:
    """The display material each of ``count`` slots must show, from the object's ``Slots``."""
    paths = contract.material_slots(payloads) or []
    return [_display_name(paths[index] if index < len(paths) else None) for index in range(count)]


def _display_name(path: str | None) -> str:
    label = path or _NO_MATERIAL
    name = DISPLAY_PREFIX + label
    if len(name.encode("utf-8")) <= _NAME_LIMIT:
        return name
    digest = hashlib.sha1(label.encode("utf-8")).hexdigest()[:8]
    # The limit is in BYTES: a character slice of a multi-byte tail would overrun it, Blender
    # would store a truncated name, and every lookup by the full one would miss.
    head, tail = f"{DISPLAY_PREFIX}…", f"#{digest}"
    budget = _NAME_LIMIT - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
    kept = label.encode("utf-8")[-budget:].decode("utf-8", errors="ignore")
    return f"{head}{kept}{tail}"


def _assign_display_materials(mesh: bpy.types.Mesh, payloads, layout: ProjectLayout) -> None:
    paths = contract.material_slots(payloads) or []
    for index in range(len(mesh.materials)):
        path = paths[index] if index < len(paths) else None
        mesh.materials[index] = _display_material(path, layout)


def _display_material(path: str | None, layout: ProjectLayout) -> bpy.types.Material:
    name = _display_name(path)
    material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    # Refreshed on every use: a material document edited since the last load shows its new colour.
    colour = (material_document.base_colour(layout.resolve(path)) if path else None) or _FALLBACK_COLOUR
    material.diffuse_color = colour
    principled = material.node_tree.nodes.get("Principled BSDF") if material.node_tree else None
    if principled is not None:
        principled.inputs["Base Color"].default_value = colour
    return material


def refresh_display(scene: bpy.types.Scene, layout: ProjectLayout | None) -> None:
    """Show each owned mesh's slots as the document now binds them -- after a save wrote a
    ``Slots`` edit, the slots would otherwise keep showing the old materials and the next save
    would refuse them as reordered."""
    if layout is None:
        return
    for obj in _editing_objects(scene):
        _assign_display_materials(obj.data, _payloads(obj), layout)


def _payloads(obj: bpy.types.Object) -> list:
    """The object's component payloads, as :func:`contract.material_slots` reads them."""
    return [component.get("data") for component in store.component_json(obj) if isinstance(component, dict)]


# -- fingerprint, export ---------------------------------------------------------------------------

def fingerprint(obj: bpy.types.Object, depsgraph) -> str:
    """A digest of the geometry the exporter would write for ``obj``: topology, every attribute,
    the corner normals and the slot count, after modifiers. Equal digests export equal GLBs, which
    is what lets an untouched scene save without writing a byte."""
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        digest = hashlib.sha256()
        digest.update(np.array(
            [len(mesh.vertices), len(mesh.loops), len(mesh.polygons), len(evaluated.material_slots)],
            dtype=np.int64,
        ).tobytes())
        _feed(digest, mesh.polygons, "loop_start", 1, np.int32)
        _feed(digest, mesh.loops, "vertex_index", 1, np.int32)
        for attribute in sorted(mesh.attributes, key=lambda item: item.name):
            if attribute.name.startswith("."):
                continue   # selection, hide and other editor state -- not geometry
            digest.update(f"{attribute.name}:{attribute.domain}:{attribute.data_type};".encode())
            layout = _ATTRIBUTES.get(attribute.data_type)
            if layout is not None:
                _feed(digest, attribute.data, layout[0], layout[1], layout[2])
        _feed(digest, mesh.corner_normals, "vector", 3, np.float32)
        return digest.hexdigest()
    finally:
        evaluated.to_mesh_clear()


def _feed(digest, collection, prop: str, width: int, dtype) -> None:
    values = np.empty(len(collection) * width, dtype=dtype)
    try:
        collection.foreach_get(prop, values)
    except (TypeError, RuntimeError):
        digest.update(b"?")
        return
    digest.update(values.tobytes())


def _export(obj: bpy.types.Object, depsgraph, path: str, state: store.EditableMesh,
            layout: ProjectLayout) -> int:
    """Write what ``obj`` evaluates to (modifiers applied, in its own space) as the GLB at
    ``path``: the whole file for a mesh it owns, the shared model with its geometry replaced for
    one it does not. Returns the primitive count the file holds."""
    mesh = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph), depsgraph=depsgraph)
    name = store.document_name(obj) or obj.name
    if not state.shared:
        return _write_glb(mesh, path, name, store.guid_of(obj))
    with tempfile.TemporaryDirectory(prefix="paradise-shared-") as directory:
        scratch = os.path.join(directory, "edited.glb")
        written = _write_glb(mesh, scratch, name, None)
        if written == state.slots:
            try:
                spliced = shared_mesh.splice(gltf.read_glb(layout.resolve(state.glb)), gltf.read_glb(scratch))
            except (ValueError, KeyError, IndexError, TypeError) as error:
                raise contract.EditableMeshError(
                    f"'{obj.name}': its edit could not be written into {state.glb}: {error}") from error
            with open(path, "wb") as handle:
                handle.write(spliced)
    return written


def _write_glb(mesh: bpy.types.Mesh, path: str, name: str, owner: str | None) -> int:
    """Export ``mesh`` alone as a GLB into ``path``, owned by ``owner`` when one is given, then
    free it; returns the primitive count written. Placeholder materials: the primitives stay
    split per slot, and there are no glTF materials for anything to extract.

    The exporter runs in a private directory: it forces a ``.glb`` extension onto its path, so
    exporting straight to the ``.tmp`` beside the target would leave a stray GLB in ``assets/``
    for the watcher to adopt, and the ``.tmp`` empty.
    """
    for key in list(mesh.keys()):
        del mesh[key]   # export_extras would write them into the GLB
    scene = bpy.data.scenes.new("Paradise editable mesh")
    if owner is not None:
        scene[contract.OWNER_EXTRA] = document_guid.canonical(owner)
    carrier = bpy.data.objects.new(name, mesh)
    scene.collection.objects.link(carrier)
    try:
        with tempfile.TemporaryDirectory(prefix="paradise-mesh-") as directory:
            exported = os.path.join(directory, "mesh.glb")
            with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
                result = bpy.ops.export_scene.gltf(
                    filepath=exported, export_format="GLB", use_active_scene=True, export_yup=True,
                    export_materials="PLACEHOLDER", export_image_format="NONE", export_extras=True,
                    export_tangents=True, export_animations=False, export_skins=False,
                    export_morph=False, export_cameras=False, export_lights=False,
                )
            if result != {"FINISHED"} or not os.path.isfile(exported):
                raise contract.EditableMeshError(f"Blender could not export the mesh of '{name}'.")
            shutil.copyfile(exported, path)
    finally:
        bpy.data.objects.remove(carrier, do_unlink=True)
        bpy.data.meshes.remove(mesh)
        bpy.data.scenes.remove(scene)
    meshes = gltf.read_json(path).get("meshes") or []
    return sum(len(item.get("primitives") or []) for item in meshes if isinstance(item, dict))


def write_initial(obj: bpy.types.Object, target: str) -> int:
    """Copy the shared model ``obj`` shows into ``target``, a GLB ``obj`` owns. Returns the slot
    count. The file lands whole (temp beside it, then replace) or not at all.

    A ``.blend``/``.fbx`` model is copied from its converted GLB: that file's primitives are the
    slots the engine binds, so slot ``i`` here is the ``Slots[i]`` the placement already had."""
    collection = obj.instance_collection
    source = collection.get(SOURCE_KEY) if collection is not None else None
    if not isinstance(source, str) or not os.path.isfile(source):
        raise contract.EditableMeshError(f"'{obj.name}' does not show a model this scene imported.")

    name = store.document_name(obj) or obj.name
    try:
        glb = glb_of(source)
    except model_source.ConversionError as error:
        raise contract.EditableMeshError(str(error)) from error
    mesh = build_mesh(glb, name)
    slots = len(mesh.materials)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    staged = _staging_file(target)
    try:
        written = _write_glb(mesh, staged, name, store.guid_of(obj))
        if written != slots:
            raise contract.EditableMeshError(
                f"{os.path.basename(source)} has {slots} material part(s) but only {written} could be "
                "written; a part with no triangles cannot keep its material binding."
            )
        os.replace(staged, target)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)
    return slots


def _staging_file(target: str) -> str:
    """A temp file in the target's own directory: ``os.replace`` is atomic only within one
    filesystem, and ``*.tmp`` is in a project's default ``[assets] ignore``, so the watcher never
    mints an identity for a file that is about to be renamed."""
    with tempfile.NamedTemporaryFile(dir=os.path.dirname(target), suffix=".tmp", delete=False) as handle:
        return handle.name


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# -- editing a shared model --------------------------------------------------------------------------

def shared_source(obj: bpy.types.Object, layout: ProjectLayout) -> str:
    """The shared GLB the instance ``obj`` shows, if it can be edited in place; else raise."""
    collection = obj.instance_collection
    source = collection.get(SOURCE_KEY) if collection is not None else None
    if not isinstance(source, str) or not os.path.isfile(source):
        raise contract.EditableMeshError(f"'{obj.name}' does not show a model this scene imported.")
    if not contract.is_inside(source, layout.assets):
        raise contract.EditableMeshError(f"{source} is outside {layout.assets}.")
    if model_source.is_converted(source):
        raise contract.EditableMeshError(model_source.edit_in_place_refusal(source))
    problem = shared_mesh.unsupported(gltf.read_json(source))
    if problem is not None:
        raise contract.EditableMeshError(f"{os.path.basename(source)} cannot be edited in place: {problem}.")
    return source


def shared_editor(scene: bpy.types.Scene, glb: str) -> bpy.types.Object | None:
    """The object in ``scene`` already editing the shared GLB ``glb`` (assets-relative), if any.
    One per model: two would each save over the other's edit."""
    for obj in _editing_objects(scene):
        state = store.editable_of(obj)
        if state.shared and state.glb == glb:
            return obj
    return None


def begin_shared(obj: bpy.types.Object, source: str, layout: ProjectLayout) -> bpy.types.Object:
    """A mesh object standing in for the instance ``obj``, editing the shared GLB ``source``.

    Carries ``obj``'s identity and is linked beside it, so the reload the caller runs next
    stashes it and hands it back in ``obj``'s place (:func:`materialize_shared`) -- the reload,
    not this function, retires ``obj`` and tags the stand-in from the document.
    """
    name = store.document_name(obj) or obj.name
    mesh = build_mesh(source, name)
    _assign_display_materials(mesh, _payloads(obj), layout)
    editor = bpy.data.objects.new(obj.name, mesh)
    editor[store.GUID_KEY] = store.guid_of(obj)
    editor[store.COMPONENTS_KEY] = obj.get(store.COMPONENTS_KEY, "[]")
    store.tag_editable(editor, store.EditableMesh(
        layout.relative(source), _sha256(source), None, len(mesh.materials), shared=True))
    obj.users_collection[0].objects.link(editor)
    return editor


# -- save ------------------------------------------------------------------------------------------

def publish(scene: bpy.types.Scene, layout: ProjectLayout | None, warn=None) -> int:
    """Write every edited mesh whose geometry changed back to its GLB; how many were written.

    All-or-nothing up to the final replace: every refusal is checked and every export staged
    before any GLB is touched, so a save that refuses has written nothing. ``warn`` hears about
    slots rearranged with no geometry change: nothing is written, and the display refresh after
    the save puts them back, so the author is told rather than silently reverted.
    """
    owned = _editing_objects(scene)
    if not owned or layout is None:
        return 0

    for obj in owned:
        if obj.mode == "EDIT":
            # Ctrl+S reaches save_pre before Blender flushes Edit Mode into the mesh.
            obj.update_from_editmode()
    view_layer = scene.view_layers[0]
    view_layer.update()
    depsgraph = view_layer.depsgraph

    changed = []
    for obj in owned:
        state = store.editable_of(obj)
        geometry = fingerprint(obj, depsgraph)
        if geometry == state.geometry:
            if warn is not None and _slots_changed(obj, state):
                warn(f"'{obj.name}': its material slots were rearranged; the game binds materials by "
                     "slot order, so they were put back. Change a slot's material in the Components "
                     "panel.")
            continue
        _refuse_changed_on_disk(obj, state, layout)
        _refuse_changed_slots(obj, state)
        changed.append((obj, state, geometry))

    staged: list[str] = []
    try:
        for obj, state, _geometry in changed:
            staged.append(_staging_file(layout.resolve(state.glb)))
            written = _export(obj, depsgraph, staged[-1], state, layout)
            if written != state.slots:
                raise contract.EditableMeshError(
                    f"'{obj.name}': {state.slots - written} of its {state.slots} material slot(s) "
                    "have no faces, so the game would bind every material after them to the wrong "
                    "part. Give each slot at least one face."
                )
        for (obj, state, geometry), temporary in zip(changed, staged, strict=True):
            path = layout.resolve(state.glb)
            shutil.copymode(path, temporary)
            os.replace(temporary, path)
            store.tag_editable(obj, replace(state, sha256=_sha256(path), geometry=geometry))
    finally:
        for temporary in staged:
            if os.path.exists(temporary):
                os.unlink(temporary)
    _reimport_shared(scene, [layout.resolve(state.glb) for _obj, state, _g in changed if state.shared])
    return len(changed)


def _reimport_shared(scene: bpy.types.Scene, paths: list[str]) -> None:
    """Show a rewritten shared model on every other placement of it now, not at the next load:
    re-import it and point each instance of the stale import at the fresh one.

    Only from Object Mode. Blender's glTF importer leaves Edit Mode and reselects what it made,
    and a Ctrl+S from Edit Mode must not throw the author out of it; the library re-imports a
    GLB whose stamp moved on the next load anyway, so waiting costs only a stale preview."""
    if not paths or bpy.context.mode != "OBJECT":
        return
    view_layer = bpy.context.view_layer
    selected = [obj for obj in scene.objects if obj.select_get()]
    active = view_layer.objects.active if view_layer is not None else None
    try:
        for path in paths:
            users = [obj for obj in bpy.data.objects
                     if obj.instance_collection is not None
                     and _same_path(obj.instance_collection.get(SOURCE_KEY), path)]
            fresh = MeshLibrary(scene).collection_for(path)
            for obj in users:
                obj.instance_collection = fresh
    finally:
        for obj in scene.objects:
            obj.select_set(obj in selected)
        if view_layer is not None:
            view_layer.objects.active = active


def _same_path(stored, path: str) -> bool:
    return isinstance(stored, str) and os.path.normcase(os.path.abspath(stored)) == os.path.normcase(
        os.path.abspath(path))


def _refuse_changed_on_disk(obj, state: store.EditableMesh, layout: ProjectLayout) -> None:
    path = layout.resolve(state.glb)
    if not os.path.isfile(path):
        raise contract.EditableMeshError(
            f"'{obj.name}': its mesh {state.glb} is gone from disk. Reload to see the document "
            "as it is now."
        )
    if _sha256(path) != state.sha256:
        raise contract.EditableMeshError(
            f"'{obj.name}': {state.glb} changed on disk since this scene read it, and saving would "
            "overwrite that change. Reload to take it; your edits to this mesh are discarded."
        )


def _slots_changed(obj, state: store.EditableMesh) -> bool:
    expected = display_names(_payloads(obj), state.slots)
    shown = [slot.material.name if slot.material is not None else None for slot in obj.material_slots]
    return shown != expected


def _refuse_changed_slots(obj, state: store.EditableMesh) -> None:
    if _slots_changed(obj, state):
        raise contract.EditableMeshError(
            f"'{obj.name}': its material slots changed ({len(obj.material_slots)} now, {state.slots} when it "
            "was loaded, or in another order). The game binds its Materials entries to the mesh "
            "by slot order, so keep the slots as they were -- one per entry, each showing its "
            "entry's material -- and change a slot's material in the Components panel."
        )


def _editing_objects(scene: bpy.types.Scene) -> list[bpy.types.Object]:
    """The objects editing a mesh: one they own, or a shared model. An owned mesh is the
    document's own object's; a shared model may be edited from a prefab's child as well, since
    its GLB, not any document, is what the save writes."""
    found = []
    for obj in scene.collection.all_objects:
        if obj.type != "MESH" or store.guid_of(obj) is None:
            continue
        state = store.editable_of(obj)
        if state is not None and (state.shared or not store.is_derived(obj)):
            found.append(obj)
    return found


# -- load --------------------------------------------------------------------------------------------

def stash(scene: bpy.types.Scene) -> dict[str, bpy.types.Object]:
    """Take the editing objects out of the scene before a load clears it, keyed by guid, so the
    load can hand each back instead of rebuilding it from its GLB."""
    kept = {}
    for obj in _editing_objects(scene):
        for collection in list(obj.users_collection):
            collection.objects.unlink(obj)
        kept[store.guid_of(obj)] = obj
    return kept


def drop(kept: dict[str, bpy.types.Object]) -> None:
    """Delete what the load did not hand back, and its mesh with it."""
    for obj in kept.values():
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    kept.clear()


def materialize(entry, source: str, layout: ProjectLayout, kept: dict, warn) -> bpy.types.Object | None:
    """The object for ``entry``, which owns the GLB at ``source``; ``None`` when the GLB cannot
    be rebuilt here, for the caller to show it as an ordinary instance instead.

    The object this scene had for it is handed back when the GLB is byte-for-byte what that
    object last wrote or read: whatever the author did that a GLB cannot hold is still there.
    """
    relative = layout.relative(source)
    sha256 = _sha256(source)
    previous = _hand_back(entry, relative, sha256, kept, layout, shared=False)
    if previous is not None:
        return previous

    try:
        mesh = build_mesh(source, entry.name or "mesh")
    except contract.EditableMeshError as error:
        warn(f"{entry.name}: {error} It is shown read-only.")
        return None
    _assign_display_materials(mesh, [c.data for c in entry.components], layout)
    obj = bpy.data.objects.new(entry.name or "object", mesh)
    store.tag_editable(obj, store.EditableMesh(relative, sha256, None, len(mesh.materials)))
    return obj


def materialize_shared(entry, source: str, layout: ProjectLayout, kept: dict) -> bpy.types.Object | None:
    """The object this scene had editing the shared GLB ``source`` for ``entry``, handed back
    while the GLB is byte-for-byte what it last read or wrote; ``None`` otherwise, for the caller
    to show an ordinary instance. Nothing in any document records the edit: it lives in this
    scene until its object is finished or the GLB changes under it."""
    guid = document_guid.canonical(entry.guid)
    state = store.editable_of(kept[guid]) if guid in kept else None
    if state is None or not state.shared:
        return None
    return _hand_back(entry, layout.relative(source), _sha256(source), kept, layout, shared=True)


def _hand_back(entry, relative: str, sha256: str, kept: dict, layout: ProjectLayout, *, shared: bool):
    previous = kept.pop(document_guid.canonical(entry.guid), None)
    if previous is None:
        return None
    state = store.editable_of(previous)
    if state is not None and state.shared == shared and state.glb == relative and state.sha256 == sha256:
        previous.parent = None
        previous.matrix_parent_inverse.identity()
        store.clear_object(previous)   # the load re-tags; nothing stale may answer for it
        store.tag_editable(previous, state)
        _assign_display_materials(previous.data, [c.data for c in entry.components], layout)
        return previous
    drop({entry.guid: previous})
    return None


def settle(objects, scene: bpy.types.Scene) -> None:
    """Fingerprint the owned objects a load just built, now that the depsgraph has them: the
    baseline the next save compares against."""
    pending = [
        obj for obj in objects
        if (state := store.editable_of(obj)) is not None and state.geometry is None
    ]
    if not pending:
        return
    depsgraph = scene.view_layers[0].depsgraph
    for obj in pending:
        state = store.editable_of(obj)
        store.tag_editable(obj, replace(state, geometry=fingerprint(obj, depsgraph)))
