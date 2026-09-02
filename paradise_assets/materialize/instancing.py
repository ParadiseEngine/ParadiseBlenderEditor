"""Placing a new prefab instance: the same markers and display :mod:`load` would have built, so
a dropped object behaves like one that was always there. A new object has no document entry, so
it carries its prefab reference on itself until the first save (:data:`store.PREFAB_KEY`).
"""

from __future__ import annotations

import os
import uuid

import bpy
from mathutils import Vector

from ..document import assets, project, resolve, schema
from ..document.prefab import PrefabDocumentError
from ..document.prefab import loads as parse_document
from . import store
from .meshes import MeshLibrary

__all__ = ["InstanceError", "add_instance", "adopt_template"]


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

    # A fresh uuid4: unlike a resolved child, there is nothing to derive it from.
    instance_guid = str(uuid.uuid4())
    store.tag_object(obj, instance_guid, _components(root))
    store.tag_name(obj, obj.name)
    store.tag_prefab(obj, guid, relative)

    try:
        _parent_to_document_root(obj, scene)
    except InstanceError:
        bpy.data.objects.remove(obj, do_unlink=True)
        raise
    _show_prefab_mesh(obj, document, layout, prefab_path)
    return obj


def adopt_template(
    scene: bpy.types.Scene,
    obj: bpy.types.Object,
    prefab_guid: str,
    relative: str,
    layout: project.ProjectLayout,
) -> bool:
    """Turn a dropped object into an instance in place (the same end state as
    :func:`add_instance`); False leaves it an ordinary empty when the prefab cannot be read."""
    path = layout.resolve(relative)
    try:
        with open(path, encoding="utf-8") as handle:
            document = parse_document(handle.read(), path)
    except (OSError, PrefabDocumentError):
        return False

    root = document.root()
    if obj.name.startswith("Empty") or not obj.name:
        obj.name = root.name or os.path.splitext(os.path.basename(relative))[0]

    store.tag_object(obj, str(uuid.uuid4()), _components(root))
    store.tag_name(obj, obj.name)
    store.tag_prefab(obj, prefab_guid, relative)

    _parent_to_document_root(obj, scene)
    _show_prefab_mesh(obj, document, layout, path)
    return True


def _components(root) -> list:
    """The prefab root's components for the panel: what a reload would show."""
    return [
        {"id": component.id, "type": component.type, "data": component.data}
        for component in root.components
    ]


def _prefab_guid(prefab_path: str) -> str | None:
    """The prefab's identity, from its sidecar -- the only place identity lives."""
    return assets.read_sidecar_guid(prefab_path + ".meta")


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
    """Parent to the document root (an unparented object would be a second root, which the
    reader refuses) and move into the root's collection so the Outliner shows it inside the
    level. Refuses when there is not exactly one root: placing anyway would save a document the
    next load rejects (#32)."""
    # `obj` itself has an identity and no parent yet; counting it would always find two roots.
    roots = [
        candidate for candidate in scene.collection.all_objects
        if candidate is not obj
        and store.guid_of(candidate) is not None
        and not store.is_derived(candidate)
        and candidate.parent is None
    ]
    if len(roots) != 1:
        names = ", ".join(repr(root.name) for root in roots) or "none"
        raise InstanceError(
            f"the document must have exactly one root object to place under; it has "
            f"{len(roots)} ({names}). Parent the extra roots first, or reload."
        )

    root = roots[0]
    obj.parent = root
    # Identity, so the visible placement IS the local transform written (as load does).
    obj.matrix_parent_inverse.identity()

    _link_beside(obj, root)


def _link_beside(obj: bpy.types.Object, root: bpy.types.Object) -> None:
    """Move ``obj`` into exactly the collections ``root`` is in."""
    targets = list(root.users_collection)
    if not targets or set(targets) == set(obj.users_collection):
        return

    for collection in list(obj.users_collection):
        collection.objects.unlink(obj)
    for collection in targets:
        collection.objects.link(obj)


def _show_prefab_mesh(obj, document, layout, prefab_path) -> None:
    """Give the object the prefab's mesh, through the loader's resolver so a nested prefab
    shows its actual root mesh."""
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
