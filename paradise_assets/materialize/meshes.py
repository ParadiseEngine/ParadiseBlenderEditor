"""Each referenced GLB imported ONCE into a hidden library collection, instanced per object
(ShiningPie: ~117 files across 225 objects). Instancing also makes the geometry uneditable in
place, which is right: the GLB owns geometry, and an edit here would vanish on the next load.
"""

from __future__ import annotations

import os

import bpy

from . import store

__all__ = ["LIBRARY_COLLECTION", "MeshLibrary"]

#: One collection per imported GLB, excluded from the view layer.
LIBRARY_COLLECTION = "ParadiseAssets/Library"

SOURCE_KEY = "paradise_glb_source"

#: ``(mtime, size)`` at import; a moved stamp means re-import.
STAMP_KEY = "paradise_glb_stamp"


class MeshLibrary:
    """Imports GLBs on demand and hands back a collection to instance."""

    def __init__(self, scene: bpy.types.Scene, warn=None) -> None:
        self._scene = scene
        self._warn = warn or (lambda message: None)
        self._by_path: dict[str, bpy.types.Collection | None] = {}
        self._root = _library_root(scene)

    @property
    def imported(self) -> int:
        """How many distinct GLBs were imported (a failed import does not count)."""
        return sum(1 for value in self._by_path.values() if value is not None)

    @property
    def sources(self) -> set[str]:
        """The GLBs actually read, so a cache can key on what a load TOUCHED rather than a
        second reference-discovery that goes stale silently."""
        return {path for path, value in self._by_path.items() if value is not None}

    def collection_for(self, path: str) -> bpy.types.Collection | None:
        """The collection for ``path``, importing on first use; ``None`` leaves the object an
        empty, since a placement whose mesh is missing is still authored data."""
        key = os.path.normcase(os.path.abspath(path))
        if key in self._by_path:
            return self._by_path[key]

        collection = self._import(path)
        self._by_path[key] = collection
        return collection

    def _import(self, path: str) -> bpy.types.Collection | None:
        if not os.path.isfile(path):
            self._warn(f"mesh not found: {path}")
            return None

        name = f"GLB/{os.path.splitext(os.path.basename(path))[0]}"
        stamp = store.stamp_of(path)
        existing = bpy.data.collections.get(name)
        if existing is not None and _is_current_import(existing, path, stamp):
            return existing
        if existing is not None and _same_source(existing, path):
            # Drop the stale collection, or the import lands on GLB/Foo.001 and leaks the old mesh.
            _discard_library_collection(existing)

        # The importer cannot be redirected; diff the object table, since names get suffixed.
        before = set(bpy.data.objects)
        try:
            bpy.ops.import_scene.gltf(filepath=path)
        except RuntimeError as error:
            self._warn(f"could not import {os.path.basename(path)}: {error}")
            return None

        created = [obj for obj in bpy.data.objects if obj not in before]
        if not created:
            self._warn(f"{os.path.basename(path)} imported nothing")
            return None

        collection = bpy.data.collections.new(name)
        collection[SOURCE_KEY] = os.path.abspath(path)
        collection[STAMP_KEY] = stamp
        self._root.children.link(collection)

        for obj in created:
            for parent in list(obj.users_collection):
                parent.objects.unlink(obj)
            collection.objects.link(obj)

        tint_by_object_colour(created)
        return collection


def tint_by_object_colour(objects) -> None:
    """Wire Object Info colour into an UNTEXTURED material's Base Color: instances share one
    mesh, so per-instance colour must come from the object. Textured materials are left alone,
    since tinting an author's texture would invent a look the game does not have."""
    for material in {slot.material for obj in objects for slot in obj.material_slots if slot.material}:
        if not material.use_nodes:
            continue

        principled = next((n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if principled is None:
            continue

        base = principled.inputs.get("Base Color")
        if base is None or base.is_linked:
            continue   # textured, or already driven by something -- not ours to touch

        info = material.node_tree.nodes.new("ShaderNodeObjectInfo")
        info.location = (principled.location.x - 300, principled.location.y)
        material.node_tree.links.new(info.outputs["Color"], base)


def _library_root(scene: bpy.types.Scene) -> bpy.types.Collection:
    """The library collection, EXCLUDED from the view layer (not hidden) so its objects are
    never evaluated."""
    root = bpy.data.collections.get(LIBRARY_COLLECTION)
    if root is None:
        root = bpy.data.collections.new(LIBRARY_COLLECTION)

    if root.name not in {child.name for child in scene.collection.children}:
        scene.collection.children.link(root)

    layer = _find_layer_collection(_view_layer_collection(scene), root.name)
    if layer is not None:
        layer.exclude = True
    return root


def _view_layer_collection(scene: bpy.types.Scene):
    """The view-layer collection, or ``None``: ``load_post`` can run before
    ``bpy.context.view_layer`` is set, and treating that as a missing library re-imports everything."""
    view_layer = getattr(bpy.context, "view_layer", None)
    if view_layer is None:
        view_layers = getattr(scene, "view_layers", None)
        view_layer = view_layers[0] if view_layers else None
    return None if view_layer is None else view_layer.layer_collection


def _is_current_import(collection: bpy.types.Collection, path: str, stamp: str) -> bool:
    return _same_source(collection, path) and collection.get(STAMP_KEY) == stamp


def _same_source(collection: bpy.types.Collection, path: str) -> bool:
    stored = collection.get(SOURCE_KEY)
    if not isinstance(stored, str) or not stored:
        return False
    return os.path.normcase(os.path.abspath(stored)) == os.path.normcase(os.path.abspath(path))


def _discard_library_collection(collection: bpy.types.Collection) -> None:
    for obj in list(collection.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(collection)


def _find_layer_collection(layer, name: str):
    if layer is None:
        return None
    if layer.collection.name == name:
        return layer
    for child in layer.children:
        found = _find_layer_collection(child, name)
        if found is not None:
            return found
    return None
