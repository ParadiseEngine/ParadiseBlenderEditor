"""A document object that OWNS its geometry: one GLB per object, written from Blender.

A shared model -- a primitive, an artist's prop -- is shown by instancing its imported GLB, which
is why no placement of it can be edited: every placement IS the same mesh. Making one editable
copies the geometry into a GLB of its own and points the object's mesh field at the ``.mesh``
document the watcher mints for that GLB. From then on the object's Blender mesh is the editor and
the GLB is the truth; the save writes the GLB back whenever the geometry changed.

**Ownership is recorded IN THE GLB** -- ``scenes[scene].extras.paradise_mesh_owner`` holds the
owning object's guid -- not in the document and not in a path convention. The document format
stays exactly as the engine reads it, ``paradise assets mv`` cannot break the link, and anything
else that references the same ``.mesh`` keeps seeing an ordinary shared instance. The GLB is
written with placeholder materials (primitives split per slot, no ``materials`` array): the game
binds materials from the object's ``Slots``, and a GLB with no materials has nothing for
``paradise assets extract`` to produce, so ``verify`` does not ask for it.

**Materials bind BY POSITION.** Engine ``MeshBlob``: "MaterialSlot i is glTF primitive i", in the
order ``GltfSceneReader.BakeInstances`` visits mesh nodes -- depth-first from the default scene's
roots, children in the order the node lists them. :func:`mesh_instances` is that walk, so the
geometry Blender edits is laid out slot for slot the way the game binds it.

Imports no ``bpy``.
"""

from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Iterable

from . import gltf, mesh_document, sidecar
from . import guid as document_guid
from .asset_reference import AssetReference
from .geometry_prefab import extraction_directories
from .new_prefab import CreateError
from .prefab import PrefabComponent
from .project import ProjectLayout
from .schema import MeshFields

__all__ = [
    "MESH_WAIT_SECONDS",
    "OWNER_EXTRA",
    "EditableMeshError",
    "material_slots",
    "mesh_field",
    "mesh_instances",
    "mesh_reference",
    "owner_of",
    "owns",
    "plan_target",
    "unsupported",
    "wait_for_mesh_reference",
]

#: The scene extra naming the object that owns a GLB.
OWNER_EXTRA = "paradise_mesh_owner"

#: How long to wait for the watcher to mint the ``.mesh`` document of a new GLB. Longer than a
#: sidecar's wait: the watcher mints the GLB's own sidecar first and the document after it, and
#: may be mid-build when the file lands.
MESH_WAIT_SECONDS = 30.0

#: glTF primitive mode 4. The only one the engine's mesh cook reads.
_TRIANGLES = 4

#: Extensions that change how geometry is STORED. A reader that ignored one would rebuild
#: garbage, so a model that requires one is refused instead.
_GEOMETRY_EXTENSIONS = frozenset({
    "KHR_draco_mesh_compression", "EXT_meshopt_compression", "KHR_mesh_quantization",
})

_IDENTITY = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


class EditableMeshError(Exception):
    """A mesh could not be made editable or written back. The message is for the author."""


#: path -> (mtime_ns, size, owner). The load asks once per referenced GLB, the menu per redraw.
_OWNERS: dict[str, tuple[int, int, str | None]] = {}


def owner_of(path: str) -> str | None:
    """The guid of the object that owns the GLB at ``path``, or ``None`` for a shared model."""
    try:
        stat = os.stat(path)
    except OSError:
        return None

    cached = _OWNERS.get(path)
    if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
        return cached[2]

    owner = _owner_in(gltf.read_json(path))
    _OWNERS[path] = (stat.st_mtime_ns, stat.st_size, owner)
    return owner


def owns(path: str, guid: str | None) -> bool:
    """Whether the object ``guid`` owns the GLB at ``path``."""
    owner = owner_of(path)
    return owner is not None and guid is not None and owner == document_guid.canonical(guid)


def _owner_in(document: dict) -> str | None:
    scene = _default_scene(document)
    extras = scene.get("extras") if scene is not None else None
    value = extras.get(OWNER_EXTRA) if isinstance(extras, dict) else None
    return document_guid.canonical(value) if document_guid.is_text(value) else None


def _default_scene(document: dict) -> dict | None:
    scenes = document.get("scenes")
    index = document.get("scene", 0)
    if not isinstance(scenes, list) or not isinstance(index, int) or not 0 <= index < len(scenes):
        return None
    scene = scenes[index]
    return scene if isinstance(scene, dict) else None


def unsupported(document: dict) -> str | None:
    """Why this GLB's geometry cannot be rebuilt primitive by primitive, or ``None``."""
    if not document:
        return "it is not a readable GLB"
    if document.get("skins"):
        return "it is rigged; a skinned mesh is edited in its source file"
    if document.get("animations"):
        return "it is animated; its clips would be lost"
    required = set(document.get("extensionsRequired") or ()) & _GEOMETRY_EXTENSIONS
    if required:
        return f"it stores geometry compressed ({', '.join(sorted(required))})"
    meshes = document.get("meshes") or []
    for node, mesh, _world in mesh_instances(document):
        primitives = meshes[mesh].get("primitives") if isinstance(meshes[mesh], dict) else None
        if not isinstance(primitives, list):
            return f"node {node} names a mesh with no primitives list"
        for primitive in primitives:
            if not isinstance(primitive, dict) or primitive.get("mode", _TRIANGLES) != _TRIANGLES:
                return "it has points or lines; only triangles are a mesh the game draws"
            attributes = primitive.get("attributes")
            if not isinstance(attributes, dict) or "POSITION" not in attributes:
                return "a primitive has no POSITION"
    return None


def mesh_instances(document: dict) -> list[tuple[int, int, tuple[float, ...]]]:
    """``(node index, mesh index, world matrix)`` per mesh-bearing node, in the order the engine
    bakes instances -- its draw slot order. The matrix is glTF's own column-major 16-tuple.

    Depth-first from the default scene's roots, each node before its children, children in the
    order the node lists them (NOT ascending index: the engine reverses the push so a stack
    visits them in document order). A malformed graph -- an index out of range, a cycle, a
    shared child -- yields what was visited before it; :func:`unsupported` has the caller refuse
    a GLB the engine itself would refuse to cook.
    """
    nodes = document.get("nodes")
    scene = _default_scene(document)
    meshes = document.get("meshes")
    if not isinstance(nodes, list) or scene is None:
        return []
    mesh_count = len(meshes) if isinstance(meshes, list) else 0

    found: list[tuple[int, int, tuple[float, ...]]] = []
    roots = scene.get("nodes") or []
    pending = [(index, _IDENTITY) for index in reversed(roots)]
    budget = len(nodes)
    while pending:
        index, parent = pending.pop()
        budget -= 1
        if not isinstance(index, int) or not 0 <= index < len(nodes) or budget < 0:
            break
        node = nodes[index] if isinstance(nodes[index], dict) else {}
        world = _multiply(parent, _local_matrix(node))
        mesh = node.get("mesh")
        if isinstance(mesh, int) and 0 <= mesh < mesh_count:
            found.append((index, mesh, world))
        children = node.get("children") or []
        pending.extend((child, world) for child in reversed(children))
    return found


def _local_matrix(node: dict) -> tuple[float, ...]:
    """A node's local transform, column-major: its ``matrix``, else T * R * S."""
    matrix = node.get("matrix")
    if isinstance(matrix, list) and len(matrix) == 16:
        return tuple(float(value) for value in matrix)

    tx, ty, tz = _numbers(node.get("translation"), 3, (0.0, 0.0, 0.0))
    x, y, z, w = _numbers(node.get("rotation"), 4, (0.0, 0.0, 0.0, 1.0))
    sx, sy, sz = _numbers(node.get("scale"), 3, (1.0, 1.0, 1.0))
    length = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / length, y / length, z / length, w / length
    # Columns of R, each scaled by the matching S -- column-major, as glTF stores a matrix.
    return (
        (1 - 2 * (y * y + z * z)) * sx, (2 * (x * y + z * w)) * sx, (2 * (x * z - y * w)) * sx, 0.0,
        (2 * (x * y - z * w)) * sy, (1 - 2 * (x * x + z * z)) * sy, (2 * (y * z + x * w)) * sy, 0.0,
        (2 * (x * z + y * w)) * sz, (2 * (y * z - x * w)) * sz, (1 - 2 * (x * x + y * y)) * sz, 0.0,
        tx, ty, tz, 1.0,
    )


def _numbers(value, count: int, default: tuple[float, ...]) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != count:
        return default
    return tuple(float(item) for item in value)


def _multiply(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, ...]:
    """``a @ b`` for column-major 4x4 matrices."""
    return tuple(
        sum(a[k * 4 + row] * b[column * 4 + k] for k in range(4))
        for column in range(4)
        for row in range(4)
    )


def mesh_field(components: list[PrefabComponent], fields: MeshFields) -> tuple[PrefabComponent, str] | None:
    """The component and field holding the object's mesh reference, if any: the FIRST field the
    schema calls a mesh, the same one the viewport displays."""
    for component in components:
        for field, value in component.data.items():
            path = value.get("path") if isinstance(value, dict) else value
            if isinstance(path, str) and fields.is_mesh_field(component.type, field, path):
                return component, field
    return None


def material_slots(payloads: Iterable[dict | None]) -> list[str | None] | None:
    """The material document per draw slot, from the first component payload carrying ``Slots``;
    ``None`` for a null slot, and ``None`` altogether when no component binds materials. Payloads,
    not components, so a document entry (``component.data``) and the ``.blend``'s JSON snapshot
    (``component["data"]``) are read by the same rule."""
    for data in payloads:
        slots = data.get("Slots") if isinstance(data, dict) else None
        if not isinstance(slots, list):
            continue
        paths: list[str | None] = []
        for slot in slots:
            path = slot.get("path") if isinstance(slot, dict) else slot
            paths.append(path if isinstance(path, str) and path else None)
        return paths
    return None


def plan_target(layout: ProjectLayout, document_path: str, name: str, guid: str) -> str:
    """Where the object ``guid`` of the document at ``document_path`` keeps its own GLB, or raise.

    A folder named after the document, beside it (``levels/arena.prefab`` ->
    ``levels/arena/Wall_1a2b3c4d.glb``): the geometry belongs to that document, and a person
    looking for it looks there. The guid prefix keeps two objects called ``Wall`` apart; the name
    is only there to be readable, and a later rename does not move the file.

    A file already at the path is refused unless THIS object owns it -- that is a retry after the
    watcher was late, and overwriting our own export is exactly right. The ``.mesh`` namespace
    the watcher will mint into is checked the same way, since the engine refuses to mint over a
    document it did not make.
    """
    canonical = document_guid.canonical(guid)
    folder = os.path.splitext(os.path.abspath(document_path))[0]
    stem = f"{_file_stem(name)}_{canonical[:8]}"
    target = os.path.join(folder, stem + ".glb")
    if not _inside(target, layout.assets):
        raise EditableMeshError(
            f"{target} is outside {layout.assets}, so no document could reference it. Is the "
            "document's folder a link to somewhere else?"
        )

    if os.path.exists(target):
        if not owns(target, canonical):
            raise EditableMeshError(
                f"{layout.relative(target)} already exists and is not this object's mesh. Move "
                "that file away, or rename the object and try again."
            )
    elif os.path.exists(sidecar.path_for(target)):
        raise EditableMeshError(
            f"{layout.relative(target)}{sidecar.SUFFIX} exists without its GLB; a new file there "
            "would take over that identity. Delete the stray sidecar, or rename the object."
        )

    try:
        directories = extraction_directories(layout, folder, ("meshes",))
    except CreateError as error:
        raise EditableMeshError(str(error)) from error

    own = {os.path.normcase(path) for path in (target, sidecar.path_for(target))}
    prefixes = (stem.casefold() + ".", stem.casefold() + "_")
    for directory in set(directories.values()):
        if not os.path.isdir(directory):
            continue
        for entry in os.listdir(directory):
            if not entry.casefold().startswith(prefixes):
                continue
            path = os.path.join(directory, entry)
            if os.path.normcase(path) in own or _is_our_mesh(layout, path, target):
                continue
            raise EditableMeshError(
                f"{layout.relative(path)} is already in the namespace the watcher would mint this "
                f"mesh into ('{stem}'). Rename the object and try again."
            )
    return target


def _inside(path: str, root: str) -> bool:
    """Whether ``path`` lands under ``root`` once links are followed -- the rule
    ``new_prefab.refuse_target`` applies, so a symlinked folder cannot carry a GLB out."""
    real_root = os.path.realpath(root)
    try:
        return os.path.commonpath((os.path.realpath(path), real_root)) == real_root
    except ValueError:
        return False


def _file_stem(name: str) -> str:
    return _UNSAFE.sub("_", name).strip("_")[:48] or "Mesh"


def _is_our_mesh(layout: ProjectLayout, path: str, glb: str) -> bool:
    """Whether ``path`` is the ``.mesh`` document (or its sidecar) minted for ``glb``."""
    document = path[: -len(sidecar.SUFFIX)] if path.endswith(sidecar.SUFFIX) else path
    if not mesh_document.is_document(document):
        return False
    source = mesh_document.glb_for(layout, layout.relative(document))
    return source is not None and _same_file(source, glb)


def _same_file(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def mesh_reference(glb: str, layout: ProjectLayout) -> AssetReference | None:
    """The ``.mesh`` document the watcher minted for ``glb``, once it AND its identity exist.

    Found through the GLB's own sidecar, whose ``[extract]`` record lists the parts the engine
    made from it -- the one place that knows where ``[extract]`` routing put the document.
    """
    identity = sidecar.read(sidecar.path_for(glb))
    record = identity.setting("extract") if identity is not None else None
    parts = record.get("parts") if isinstance(record, dict) else None
    for part in parts if isinstance(parts, list) else ():
        if not isinstance(part, dict) or part.get("kind") != "meshes":
            continue
        relative = part.get("path")
        if not isinstance(relative, str) or not relative:
            return None
        absolute = layout.resolve(relative)
        minted = sidecar.read(sidecar.path_for(absolute))
        if minted is None or not _is_our_mesh(layout, absolute, glb):
            return None
        return AssetReference(minted.guid, relative)
    return None


def wait_for_mesh_reference(
    glb: str, layout: ProjectLayout, timeout: float = MESH_WAIT_SECONDS, interval: float = 0.1
) -> AssetReference | None:
    """Block until the watcher has minted ``glb``'s ``.mesh`` document, or ``None`` if it never
    does. Polling for the same reason :func:`sidecar.wait_for` polls: the writer is another
    process, and a file caught mid-write simply reads as not there yet."""
    deadline = time.monotonic() + timeout
    while True:
        found = mesh_reference(glb, layout)
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)
