"""Importing the GLBs a scene references, once each, and instancing them.

A scene names its meshes by path -- ``Models/Prim_Cube.glb`` -- and ShiningPie's three documents
between them reference ~117 files across 225 objects. Importing per object would mean importing
the same file dozens of times.

So each unique GLB is imported ONCE into a hidden library collection, and every object that
references it becomes a **collection instance** of that collection. Two consequences, both
wanted:

* geometry is shared, so the scene stays light no matter how many props repeat;
* the geometry is not editable in place, which is structurally the right message. **The document
  owns placement; the GLB owns geometry.** Editing a mesh here would edit something this addon
  never writes back, and the edit would vanish on the next load with no warning.

Textures arrive for free now, and that is new. ``paradise_blender``'s preview had to strip
materials because ``data/``-era GLBs carried KTX2 and Blender cannot read it -- "a grey
silhouette that is honestly the right shape beats one that claims to be the asset". The GLBs
under ``assets/`` reference their PNG sources instead, which Blender's importer reads, so a
materialized scene looks like the game.
"""

from __future__ import annotations

import os

import bpy

__all__ = ["LIBRARY_COLLECTION", "MeshLibrary"]

#: Holds one collection per imported GLB. Excluded from the view layer, so its contents are
#: never drawn directly -- only through the instances that reference them.
LIBRARY_COLLECTION = "ParadiseAssets/Library"

#: The source path an imported collection came from, for reuse across a reload.
SOURCE_KEY = "paradise_glb_source"


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
        """The GLBs this library actually read, absolute.

        Reported so a caller can key a cache on what a load TOUCHED rather than on its own reading
        of what the document references. The second one goes stale the day a component gains a
        mesh field, and a stale key does not fail loudly -- it serves last week's artifact and
        reports success. A failed import is excluded: nothing was read, so there is nothing for it
        to invalidate on, and including it would peg the cache to a file that may never appear.
        """
        return {path for path, value in self._by_path.items() if value is not None}

    def collection_for(self, path: str) -> bpy.types.Collection | None:
        """The collection holding ``path``'s contents, importing it the first time.

        Returns ``None`` when the file is missing or unreadable -- the caller leaves the object
        as an empty, which is honest: a placement whose mesh cannot be found is still a
        placement, and deleting it would lose authored data over a missing file.
        """
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
        existing = bpy.data.collections.get(name)
        if existing is not None and existing.get(SOURCE_KEY) == path:
            return existing

        # The importer drops its objects into the active collection, and there is no argument to
        # redirect it. Diffing the object table around the call is the only reliable way to learn
        # what it created -- names collide and get suffixed, so a name-based guess is wrong the
        # second time the same asset is imported.
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
        collection[SOURCE_KEY] = path
        self._root.children.link(collection)

        for obj in created:
            for parent in list(obj.users_collection):
                parent.objects.unlink(obj)
            collection.objects.link(obj)

        tint_by_object_colour(created)
        return collection


def tint_by_object_colour(objects) -> None:
    """Make an UNTEXTURED material take its base colour from the instancing object.

    A prefab instance shares one mesh with every other instance, so per-instance colour cannot
    live in the material -- it has to come from the object. Blender's Object Info node supplies
    exactly that, and wiring it into Base Color is what lets one grey cube render as fifty
    differently-coloured graybox volumes.

    **Textured materials are left alone.** A material with an image feeding Base Color is real
    art, and multiplying an author's texture by an object colour would be inventing a look the
    game does not have. This only touches materials whose Base Color is a flat value -- which is
    precisely the graybox case.
    """
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
    """The hidden collection every imported GLB lands under.

    Excluded from the view layer rather than merely hidden: an excluded collection's objects are
    not evaluated at all, so 117 imported assets cost nothing in the viewport when only their
    instances are on screen.
    """
    root = bpy.data.collections.get(LIBRARY_COLLECTION)
    if root is None:
        root = bpy.data.collections.new(LIBRARY_COLLECTION)

    if root.name not in {child.name for child in scene.collection.children}:
        scene.collection.children.link(root)

    layer = _find_layer_collection(bpy.context.view_layer.layer_collection, root.name)
    if layer is not None:
        layer.exclude = True
    return root


def _find_layer_collection(layer, name: str):
    if layer.collection.name == name:
        return layer
    for child in layer.children:
        found = _find_layer_collection(child, name)
        if found is not None:
            return found
    return None
