"""Scene document -> Blender objects.

One Blender object per document object, carrying the document's identity, placed by the
document's local TRS rebased into Blender's axes, parented as the document says, and displaying
whatever GLB its components reference.

The transform path is the part with a trap in it, and ``paradise_blender/CONVENTIONS.md``
records why: **assign the channels, never the matrix.** Setting ``matrix_basis`` makes Blender
decompose, and that round trip is lossy at ~1e-6 -- it once moved 25 of ShiningPie's 321 objects
on every export. Here the same loss would mean every object's decimals churn the moment the
scene is saved back, turning a one-object edit into a whole-file diff.
"""

from __future__ import annotations

import os

import bpy
from mathutils import Quaternion, Vector

from ..document import axes, prefab, project, schema, well_known
from ..document.scene import SceneDocument, SceneObject, loads as load_scene
from . import store
from .meshes import MeshLibrary

__all__ = ["LoadResult", "load_document"]


class LoadResult:
    """What a load produced, for the operator to report."""

    def __init__(self) -> None:
        self.objects = 0
        self.meshes = 0
        self.instances = 0
        self.derived = 0
        self.warnings: list[str] = []

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def load_document(
    scene: bpy.types.Scene,
    document: SceneDocument,
    scene_path: str,
    layout: project.ProjectLayout,
) -> LoadResult:
    """Materialize ``document`` into ``scene``, replacing anything already loaded there."""
    result = LoadResult()
    _clear_previous(scene)

    mesh_fields = schema.load(layout.root)
    if not mesh_fields.from_schema:
        result.warn(
            "no authoring-schema.json found; mesh references are detected by their .glb "
            "extension instead (build the game's launcher to get the real schema)"
        )

    # Prefab instances are expanded for DISPLAY: what the scene contains is the resolved
    # objects, and showing the unresolved instance would show an empty at the origin. The
    # unresolved document is what save writes back to, which is why the resolved children are
    # marked derived below rather than treated as ordinary objects.
    expansion = prefab.resolve(document, lambda reference: _load_prefab(layout, reference, result))
    for error in expansion.errors:
        result.warn(error)

    authored = {entry.guid for entry in document.objects if entry.guid is not None}

    library = MeshLibrary(scene, result.warn)
    created: dict[str, bpy.types.Object] = {}

    for entry in expansion.document.objects:
        obj = _create_object(entry, scene, layout, library, mesh_fields, result)
        if entry.guid not in authored:
            store.mark_derived(obj)
            result.derived += 1
        created[entry.guid] = obj
        result.objects += 1

    result.instances = expansion.expanded
    document = expansion.document

    # Parenting is a SECOND pass because a document may name a parent that appears later in the
    # file; the reader already proved every parent exists and that there are no cycles.
    for entry in document.objects:
        if entry.parent is None:
            continue
        child = created[entry.guid]
        child.parent = created[entry.parent]
        # Identity, so the stored local TRS IS the Blender local TRS. Blender's default is the
        # inverse of the parent's world matrix at parenting time, which would silently offset
        # every child by wherever its parent happened to be.
        child.matrix_parent_inverse.identity()

    result.meshes = library.imported
    store.write_state(scene, scene_path)
    return result


def _create_object(
    entry: SceneObject,
    scene: bpy.types.Scene,
    layout: project.ProjectLayout,
    library: MeshLibrary,
    mesh_fields: schema.MeshFields,
    result: LoadResult,
) -> bpy.types.Object:
    obj = bpy.data.objects.new(entry.name or "object", None)
    obj.empty_display_size = 0.25
    scene.collection.objects.link(obj)

    store.tag_object(obj, entry.guid, _components_payload(entry))
    _apply_transform(obj, entry)

    reference = _mesh_reference(entry, mesh_fields)
    if reference is not None:
        collection = library.collection_for(layout.resolve(reference))
        if collection is not None:
            obj.instance_type = "COLLECTION"
            obj.instance_collection = collection
        else:
            result.warn(f"{entry.name}: mesh '{reference}' could not be displayed")

    return obj


def _load_prefab(layout, reference, result):
    """Read a prefab a scene references, reporting rather than raising."""
    path = layout.resolve(reference.path)
    try:
        with open(path, encoding="utf-8") as handle:
            return prefab.validate(load_scene(handle.read(), path), path)
    except OSError:
        result.warn(f"prefab '{reference.path}' could not be read")
        return None
    except Exception as error:   # SceneDocumentError, reported not raised
        result.warn(str(error))
        return None


def _transform_of(entry: SceneObject):
    """The object's local TRS, out of its transform component. Identity when it has none."""
    component = entry.component(well_known.TRANSFORM_ID)
    if component is None:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)

    def numbers(key, count, default):
        value = component.data.get(key)
        if not isinstance(value, list) or len(value) != count:
            return default
        return tuple(float(v) for v in value)

    return (
        numbers(well_known.POSITION, 3, (0.0, 0.0, 0.0)),
        numbers(well_known.ROTATION, 4, (0.0, 0.0, 0.0, 1.0)),
        numbers(well_known.SCALE, 3, (1.0, 1.0, 1.0)),
    )


def _apply_transform(obj: bpy.types.Object, entry: SceneObject) -> None:
    """Place ``obj`` from the document's TRS, rebased into Blender's axes."""
    document_position, document_rotation, document_scale = _transform_of(entry)
    position, rotation, scale = axes.to_blender_trs(
        document_position, document_rotation, document_scale
    )

    obj.rotation_mode = "QUATERNION"
    obj.location = Vector(position)
    # The document is (x, y, z, w); Blender's Quaternion is (w, x, y, z).
    obj.rotation_quaternion = Quaternion((rotation[3], rotation[0], rotation[1], rotation[2]))
    obj.scale = Vector(scale)


def _components_payload(entry: SceneObject) -> list:
    """The object's components in the shape the panel and the JSON store want."""
    return [
        {"id": component.id, "type": component.type, "data": component.data}
        for component in entry.components
    ]


def _mesh_reference(entry: SceneObject, mesh_fields: schema.MeshFields) -> str | None:
    """The first mesh path the object's components name, if any.

    First rather than all: an object is one placement and gets one display mesh. A component set
    naming two would be an authoring question, not something for the loader to invent an answer
    to -- and no component in use does.

    A reference may be a bare path or an ``AssetReference`` inline table. Both are read, because
    the two forms coexist while games move over: the guid half is what a resolver would use, but
    displaying a mesh only needs somewhere to read bytes from, and the path is that.
    """
    for component in entry.components:
        for field, value in component.data.items():
            if isinstance(value, dict):
                path = value.get("path")
                if isinstance(path, str) and mesh_fields.is_mesh_field(component.type, field, path):
                    return path
            elif mesh_fields.is_mesh_field(component.type, field, value):
                return value
    return None


def _clear_previous(scene: bpy.types.Scene) -> None:
    """Remove the objects a previous load created, leaving anything else alone.

    Keyed on the GUID marker rather than on the collection, so a user's own objects added to the
    scene survive a reload. The mesh library is left in place: re-importing 117 GLBs to show the
    same geometry would make reload cost what the first load cost.
    """
    doomed = [obj for obj in scene.collection.all_objects if store.guid_of(obj) is not None]
    for obj in doomed:
        bpy.data.objects.remove(obj, do_unlink=True)


def scene_document_path(scene: bpy.types.Scene) -> str | None:
    """The document ``scene`` was materialized from, if it was."""
    state = store.read_state(scene)
    return state.path if state and os.path.isfile(state.path) else None
