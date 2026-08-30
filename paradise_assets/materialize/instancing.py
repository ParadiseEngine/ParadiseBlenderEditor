"""Placing a new prefab instance in an open document.

The counterpart to :mod:`load`, for one object rather than a whole file. It builds the same thing
load would have built had the document already contained the instance -- same markers, same
display -- so that the object a user drops behaves identically to one that was there all along,
and a save followed by a reload is a no-op rather than a surprise.

**What makes an instance an instance is its identity plus its prefab reference**, and both are set
here: a freshly minted guid the document has never seen, and the prefab's own identity read from
its sidecar. Load takes the reference from the document; a new object has no document entry yet,
so it carries the reference on itself until the first save writes one (see
:data:`store.PREFAB_KEY`).
"""

from __future__ import annotations

import os
import uuid

import bpy
from mathutils import Vector

from ..document import project, resolve, schema, well_known
from ..document.prefab import PrefabDocumentError, loads as parse_document
from . import store
from .meshes import MeshLibrary

__all__ = ["InstanceError", "add_instance"]


class InstanceError(Exception):
    """The prefab could not be placed. The message is for the author."""


def add_instance(
    scene: bpy.types.Scene,
    prefab_path: str,
    layout: project.ProjectLayout,
    location=(0.0, 0.0, 0.0),
) -> bpy.types.Object:
    """Create a Blender object instancing the prefab at ``prefab_path``."""
    try:
        with open(prefab_path, encoding="utf-8") as handle:
            document = parse_document(handle.read(), prefab_path)
    except OSError as error:
        raise InstanceError(f"could not read {os.path.basename(prefab_path)}: {error}") from error
    except PrefabDocumentError as error:
        raise InstanceError(str(error)) from error

    guid = _prefab_guid(prefab_path)
    if guid is None:
        raise InstanceError(
            f"{os.path.basename(prefab_path)} has no sidecar, so it has no identity to reference. "
            "Run 'paradise assets verify' to find what is missing."
        )

    relative = _assets_relative(prefab_path, layout)
    if relative is None:
        raise InstanceError(f"{prefab_path} is not inside {layout.assets}")

    root = document.root()
    name = root.name or os.path.splitext(os.path.basename(prefab_path))[0]

    obj = bpy.data.objects.new(name, None)
    obj.empty_display_size = 0.25
    scene.collection.objects.link(obj)
    obj.location = Vector(location)

    # A fresh uuid4: the instance is a thing the document has never contained, so unlike a
    # resolved child -- whose identity is MINTED from its instance so it stays stable across
    # loads -- there is nothing to derive it from and nothing that should collide with it.
    instance_guid = str(uuid.uuid4())
    store.tag_object(obj, instance_guid, _components(root))
    store.tag_prefab(obj, guid, relative)

    _parent_to_document_root(obj, scene)
    _show_prefab_mesh(obj, document, layout, prefab_path)
    return obj


def _components(root) -> list:
    """The prefab root's components, for the panel to show.

    Display only, and deliberately the PREFAB's rather than an empty list: an instance shows what
    it inherits, which is what the panel would show after a reload.
    """
    return [
        {"id": component.id, "type": component.type, "data": component.data}
        for component in root.components
    ]


def _prefab_guid(prefab_path: str) -> str | None:
    """The prefab's identity, from its sidecar -- the only place identity lives."""
    sidecar = prefab_path + ".meta"
    if not os.path.isfile(sidecar):
        return None

    with open(sidecar, encoding="utf-8") as handle:
        for line in handle:
            key, _, value = line.partition("=")
            if key.strip() == "guid":
                return value.strip().strip('"') or None
    return None


def _assets_relative(path: str, layout: project.ProjectLayout) -> str | None:
    """The path as a document would write it: '/'-separated, relative to assets/."""
    try:
        relative = os.path.relpath(path, layout.assets)
    except ValueError:
        return None
    if relative.startswith(".."):
        return None
    return relative.replace("\\", "/")


def _parent_to_document_root(obj: bpy.types.Object, scene: bpy.types.Scene) -> None:
    """Parent the new object to the document's root, if the scene has one loaded.

    Every document has exactly one root, and an object with no parent would BE a second one --
    which the reader refuses outright. So this is not tidiness; without it the first save produces
    a document that will not open.
    """
    # `obj` is excluded: it has an identity and no parent yet, so counting it would make every
    # document look like it already had two roots and this would never parent anything.
    roots = [
        candidate for candidate in scene.collection.all_objects
        if candidate is not obj
        and store.guid_of(candidate) is not None
        and not store.is_derived(candidate)
        and candidate.parent is None
    ]
    if len(roots) != 1:
        return

    obj.parent = roots[0]
    # Identity, so the placement the user sees is the local transform that gets written -- the
    # same reason load sets it.
    obj.matrix_parent_inverse.identity()


def _show_prefab_mesh(obj, document, layout, prefab_path) -> None:
    """Give the new object the prefab's mesh, so it looks like what it is.

    Resolved through the same resolver the loader uses, so a prefab that instantiates another
    shows the mesh of whatever is actually at its root rather than nothing.
    """
    mesh_fields = schema.load(layout.root)

    def prefabs(reference):
        try:
            with open(layout.resolve(reference.path), encoding="utf-8") as handle:
                return parse_document(handle.read(), reference.path)
        except (OSError, PrefabDocumentError):
            return None

    resolved = resolve.resolve(document, prefabs)
    if resolved.errors:
        return

    root = resolved.document.single_root() or document.root()
    for component in root.components:
        for field, value in component.data.items():
            path = value.get("path") if isinstance(value, dict) else value
            if not isinstance(path, str) or not mesh_fields.is_mesh_field(component.type, field, path):
                continue

            collection = MeshLibrary(bpy.context.scene).collection_for(layout.resolve(path))
            if collection is not None:
                obj.instance_type = "COLLECTION"
                obj.instance_collection = collection
            return
