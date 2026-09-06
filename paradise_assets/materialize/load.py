"""Prefab document -> Blender objects. Assign the transform CHANNELS, never ``matrix_basis``:
the matrix round trip is lossy at ~1e-6 (it moved 25 of ShiningPie's 321 objects per export),
and here it would churn every object's decimals on the next save.
"""

from __future__ import annotations

import os
import tomllib

import bpy
from mathutils import Quaternion, Vector

from ..document import axes, component_schema, mesh_document, project, resolve, schema, well_known
from ..document.prefab import PrefabDocument, PrefabObject
from ..document.prefab import loads as parse_document
from . import shapes, store
from .meshes import LIBRARY_COLLECTION, MeshLibrary

__all__ = ["LoadResult", "load_document"]


class LoadResult:
    """What a load produced, for the operator to report."""

    def __init__(self) -> None:
        self.objects = 0
        self.meshes = 0
        self.instances = 0
        self.derived = 0
        self.warnings: list[str] = []
        #: Every file the load READ, for the thumbnail cache to invalidate on: a second
        #: reference-discovery would drift silently and serve a stale artifact. Failed parses
        #: are recorded too, so fixing one counts as a change.
        self.sources: set[str] = set()

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def read(self, path: str) -> str:
        """Record ``path`` as read, and hand it back so call sites stay one line."""
        self.sources.add(os.path.normcase(os.path.abspath(path)))
        return path


def load_document(
    scene: bpy.types.Scene,
    document: PrefabDocument,
    scene_path: str,
    layout: project.ProjectLayout,
    *,
    clear_startup: bool = False,
) -> LoadResult:
    """Materialize ``document`` into ``scene``, replacing anything already loaded there.

    ``clear_startup`` asks for Blender's startup content to go with it (see
    :func:`_startup_content`). Opt-in, and only the operator that opens a document FOR A PERSON
    passes it: this function is also how a thumbnail is rendered, how a reload rebuilds, and how
    an extraction re-materializes, and each of those has arranged the scene it hands over."""
    result = LoadResult()
    result.read(scene_path)
    # Captured before anything changes, dropped after the clear and before anything is created:
    # the clear owns the document objects, and doing it in this order means no captured
    # reference can have been freed under us and no name is taken when the document wants it.
    startup = _startup_content(scene) if clear_startup else None
    _clear_previous(scene)
    _drop_startup_content(startup)

    mesh_fields = schema.load(layout.root)
    if not mesh_fields.from_schema:
        result.warn(
            "no authoring-schema.json found; mesh references are detected by their .glb "
            "extension instead (build the game's launcher to get the real schema)"
        )

    # Instances are expanded for DISPLAY only; the resolved children are marked derived so save
    # never writes them back.
    expansion = resolve.resolve(document, lambda reference: _load_prefab(layout, reference, result))
    for error in expansion.errors:
        result.warn(error)

    authored = {entry.guid for entry in document.objects if entry.guid is not None}

    # Which prefab each instance instantiates. Read BEFORE the expansion, which replaces an
    # instance entry with the prefab's resolved root and so consumes the reference: without this
    # the only object that knows is one added in this session (instancing.add_instance), and
    # "open the prefab this came from" would work for those alone.
    instanced = {
        entry.guid: entry.prefab for entry in document.objects
        if entry.guid is not None and entry.prefab is not None
    }

    library = MeshLibrary(scene, result.warn)
    created: dict[str, bpy.types.Object] = {}

    for entry in expansion.document.objects:
        obj = _create_object(entry, scene, layout, library, mesh_fields, result)
        if entry.guid not in authored:
            store.mark_derived(obj)
            result.derived += 1
        if (reference := instanced.get(entry.guid)) is not None:
            store.tag_prefab(obj, reference.guid, reference.path)
        created[entry.guid] = obj
        result.objects += 1

    # Shapes only for what this DOCUMENT authors. An instance's own entry is read from the
    # file, not the expansion: the expansion folds the prefab's components in, and a shape the
    # prefab declares is edited in the prefab. A resolved child is not an object at all.
    vocabulary = component_schema.load(layout.root)
    for entry in document.objects:
        if entry.guid in created:
            shapes.materialize(created[entry.guid], _components_payload(entry), vocabulary)

    result.instances = expansion.expanded
    document = expansion.document

    # Second pass: a parent may appear later in the file. An instance whose prefab could not be
    # read is not in the expansion at all, so a child hanging off it stays a root here, with the
    # resolver's warning already saying why.
    for entry in document.objects:
        if entry.parent is None:
            continue
        child = created.get(entry.guid)
        parent = created.get(entry.parent)
        if child is None or parent is None:
            result.warn(
                f"{entry.name or entry.guid}: its parent {entry.parent} could not be materialized, "
                "so it is shown unparented"
            )
            continue
        child.parent = parent
        # Identity, so the stored local TRS IS Blender's; the default parent inverse would
        # silently offset every child.
        child.matrix_parent_inverse.identity()

    result.meshes = library.imported
    result.sources |= library.sources
    store.write_state(scene, scene_path)
    return result


def _create_object(
    entry: PrefabObject,
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
    store.tag_name(obj, entry.name)
    _apply_transform(obj, entry)

    reference = _mesh_reference(entry, mesh_fields)
    if reference is not None:
        # The field names a mesh DOCUMENT; the GLB it was compiled from is what Blender imports.
        source = mesh_document.displayable(layout, reference)
        collection = library.collection_for(source) if source is not None else None
        if collection is not None:
            obj.instance_type = "COLLECTION"
            obj.instance_collection = collection
        else:
            result.warn(f"{entry.name}: mesh '{reference}' could not be displayed")

    _apply_authored_colour(obj, entry, layout, result)
    return obj


def _apply_authored_colour(
    obj: bpy.types.Object, entry: PrefabObject, layout, result: LoadResult
) -> None:
    """Put the authored material colour on ``obj.color``: instances share one mesh, so
    per-instance colour cannot live in the material, and without it every graybox is the same grey."""
    for component in entry.components:
        slots = component.data.get("Slots")
        if not isinstance(slots, list) or not slots:
            continue
        first = slots[0]
        if not isinstance(first, dict) or not first.get("path"):
            continue

        colour = _base_colour(result.read(layout.resolve(first["path"])))
        if colour is not None:
            obj.color = colour
        return


def _base_colour(path: str):
    """``BaseColorFactor`` from a material document, or ``None``."""
    try:
        with open(path, "rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None

    factor = document.get("BaseColorFactor")
    if not isinstance(factor, dict):
        return None

    return (
        float(factor.get("r", 1.0)),
        float(factor.get("g", 1.0)),
        float(factor.get("b", 1.0)),
        float(factor.get("a", 1.0)),
    )


def _load_prefab(layout, reference, result):
    """Read a prefab a scene references, reporting rather than raising."""
    path = result.read(layout.resolve(reference.path))
    try:
        with open(path, encoding="utf-8") as handle:
            return parse_document(handle.read(), path)
    except OSError:
        result.warn(f"prefab '{reference.path}' could not be read")
        return None
    except Exception as error:   # PrefabDocumentError, reported not raised
        result.warn(str(error))
        return None


def _transform_of(entry: PrefabObject):
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


def _apply_transform(obj: bpy.types.Object, entry: PrefabObject) -> None:
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


def _components_payload(entry: PrefabObject) -> list:
    """The object's components in the shape the panel and the JSON store want."""
    return [
        {"id": component.id, "type": component.type, "data": component.data}
        for component in entry.components
    ]


def _mesh_reference(entry: PrefabObject, mesh_fields: schema.MeshFields) -> str | None:
    """The first mesh path the components name (a bare path or an ``AssetReference``), if any."""
    for component in entry.components:
        for field, value in component.data.items():
            if isinstance(value, dict):
                path = value.get("path")
                if isinstance(path, str) and mesh_fields.is_mesh_field(component.type, field, path):
                    return path
            elif mesh_fields.is_mesh_field(component.type, field, value):
                return value
    return None


def _startup_content(scene: bpy.types.Scene):
    """Blender's startup file, when that is all this session is; ``None`` otherwise.

    A fresh Blender opens on the startup file, so a document materialized into it lands beside a
    Cube, a Camera, a Light and the ``Collection`` holding them -- clutter in the Outliner, and
    three objects the author has to learn are not part of the level (they are not document
    objects, so a save ignores them, which makes it worse rather than better: they persist,
    invisibly meaningless, into the working file).

    The gate is "never saved AND never touched". ``is_dirty`` is what separates the startup file
    from a session someone has done unsaved work in, and it is the same signal the Asset
    Browser's open entry asks about before replacing a session. Anything else -- a saved .blend,
    a workfile reopened, an author who modelled something first -- is content this addon has no
    business deleting, so it is left entirely alone.

    It is NOT enough on its own, which is why the caller has to ask as well. ``is_dirty`` follows
    UNDO PUSHES, not data changes: every operator a person runs makes one, and a script that
    links an object makes none. ``thumbnail.py`` is exactly that script -- it starts an empty
    file and links its own camera and key light before materializing -- so a gate that trusted
    ``is_dirty`` alone deleted the camera and rendered every prefab black.
    """
    if bpy.data.filepath or bpy.data.is_dirty:
        return None
    # Document objects are `_clear_previous`'s, not ours. A pristine GUI session holds none, but
    # a scripted one loading twice does, and capturing those meant handing already-freed
    # references to `objects.remove`.
    return (
        [obj for obj in scene.collection.all_objects if store.guid_of(obj) is None],
        list(scene.collection.children),
    )


def _drop_startup_content(captured) -> None:
    """Remove what :func:`_startup_content` found. Objects first, so a collection is empty by the
    time it goes; a collection still holding something else -- the mesh library from an earlier
    load in the same session -- is kept, because emptiness is the only claim we have on it."""
    if captured is None:
        return

    objects, collections = captured
    for obj in objects:
        bpy.data.objects.remove(obj, do_unlink=True)
    for collection in collections:
        if collection.name == LIBRARY_COLLECTION or collection.all_objects or collection.children:
            continue
        bpy.data.collections.remove(collection)


def _clear_previous(scene: bpy.types.Scene) -> None:
    """Remove a previous load's objects (by GUID marker, so the user's own survive), keeping
    the mesh library so reload does not re-import every GLB. Drops pending edits too, which is
    why ``workfile.refresh_from_document`` refuses to run this over unsaved work."""
    doomed = [
        obj for obj in scene.collection.all_objects
        if store.guid_of(obj) is not None or shapes.is_shape(obj)
    ]
    for obj in doomed:
        bpy.data.objects.remove(obj, do_unlink=True)


def scene_document_path(scene: bpy.types.Scene) -> str | None:
    """The document ``scene`` was materialized from, if it was."""
    state = store.read_state(scene)
    return state.path if state and os.path.isfile(state.path) else None
