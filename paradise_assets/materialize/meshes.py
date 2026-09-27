"""Each referenced model imported ONCE into a hidden library collection, instanced per object
(ShiningPie: ~117 files across 225 objects). Instancing also makes the geometry uneditable in
place, which is right: the model owns geometry, and an edit here would vanish on the next load.

A ``.gltf`` is imported as it is, like a ``.glb``; its stamp covers its buffer files too, so a
re-exported ``.bin`` is re-imported. A converted model (``.blend``, ``.fbx``, ``.obj`` ...) is
shown through the GLB the pipeline converts it to (``document/model_source.py``), but the library
keys, names and stamps the collection by the SOURCE plus that GLB's stamp: saving the ``.blend``
-- or an ``.obj``'s ``.mtl`` -- is what makes the next load convert and re-import it.

One ``.blend`` may hold several models, one per asset collection: each is its own library
collection, keyed by the source AND the asset, imported from that asset's own GLB.
"""

from __future__ import annotations

import os

import bpy

from ..document import gltf, model_source, project
from ..play import host
from . import store

__all__ = ["LIBRARY_COLLECTION", "MeshLibrary", "glb_of", "model_of"]

#: One collection per imported model, excluded from the view layer.
LIBRARY_COLLECTION = "ParadiseAssets/Library"

#: The model source the collection shows: the ``.glb`` or ``.gltf`` imported, or the converted
#: source whose GLB was. The key predates converted sources; renaming it would orphan every
#: existing workfile's library.
SOURCE_KEY = "paradise_glb_source"

#: The asset of a multi-asset source the collection shows; absent for a whole-file model.
ASSET_KEY = "paradise_glb_asset"

#: ``(mtime, size)`` at import -- of the source and a ``.gltf``'s buffer files, and of the
#: converted GLB when there is one; a moved stamp means re-import.
STAMP_KEY = "paradise_glb_stamp"


def glb_of(source: str, asset: str | None = None) -> str:
    """The GLB to read for the model ``source`` (or its ``asset``), converting a source whose
    converted GLB is missing or stale -- the source or a file it depends on changed -- through
    ``paradise assets convert``. Synchronous: the load needs the file before it can show
    anything, and a current conversion costs no process at all.

    Raises :class:`model_source.ConversionError` with the reason, for the caller to report."""
    found = model_source.current_glb(source, asset)
    if found is not None:
        return found
    name = model_source.Model(source, asset).label
    if not model_source.is_converted(source):
        if asset is None:
            return source
        raise model_source.ConversionError(
            f"{name}: a {os.path.splitext(source)[1]} holds one model, not assets")
    layout = project.locate(source)
    if layout is None:
        raise model_source.ConversionError(
            f"{name} is not inside an asset project, so it cannot be converted")
    if asset is not None and not model_source.is_asset_name(asset):
        raise model_source.ConversionError(f"'{asset}' cannot name an asset of {os.path.basename(source)}")
    result = host.run_cli(model_source.convert_arguments(layout, source, asset), layout.root)
    if result is None:
        raise model_source.ConversionError(
            f"{name} needs converting to a GLB, and the Paradise CLI could not be started. Set it "
            "in the addon preferences.")
    if not result.ok:
        raise model_source.ConversionError(f"could not convert {name} to a GLB: {result.summary()}")
    glb = model_source.printed_path(result.stdout)
    if glb is None or not os.path.isfile(glb):
        raise model_source.ConversionError(
            f"`paradise assets convert` finished without naming the GLB it wrote for {name}")
    if not model_source.is_current(source, glb, asset):
        # The .blend converted fine, just not to this model: its asset collections were renamed,
        # removed, or added since the reference was extracted.
        raise model_source.ConversionError(
            f"{os.path.basename(source)} has no asset collection named '{asset}'" if asset is not None
            else f"{name} now holds asset collections; re-extract it and place one of their prefabs")
    return glb


def model_of(collection: bpy.types.Collection | None) -> model_source.Model | None:
    """The model a library collection shows, or ``None`` for any other collection."""
    source = collection.get(SOURCE_KEY) if collection is not None else None
    if not isinstance(source, str):
        return None
    asset = collection.get(ASSET_KEY)
    return model_source.Model(source, asset if isinstance(asset, str) else None)


class MeshLibrary:
    """Imports models on demand and hands back a collection to instance."""

    def __init__(self, scene: bpy.types.Scene, warn=None) -> None:
        self._scene = scene
        self._warn = warn or (lambda message: None)
        self._by_path: dict[tuple[str, str | None], bpy.types.Collection | None] = {}
        self._root = _library_root(scene)

    @property
    def imported(self) -> int:
        """How many distinct models were imported (a failed import does not count)."""
        return sum(1 for value in self._by_path.values() if value is not None)

    @property
    def sources(self) -> set[str]:
        """The model sources actually read, so a cache can key on what a load TOUCHED rather
        than a second reference-discovery that goes stale silently."""
        return {path for (path, _asset), value in self._by_path.items() if value is not None}

    def collection_for(self, path: str, asset: str | None = None) -> bpy.types.Collection | None:
        """The collection for the model ``path`` (or its ``asset``), importing on first use;
        ``None`` leaves the object an empty, since a placement whose mesh is missing is still
        authored data."""
        key = (os.path.normcase(os.path.abspath(path)), asset)
        if key in self._by_path:
            return self._by_path[key]

        collection = self._import(path, asset)
        self._by_path[key] = collection
        return collection

    def _import(self, path: str, asset: str | None) -> bpy.types.Collection | None:
        if not os.path.isfile(path):
            self._warn(f"mesh not found: {path}")
            return None

        label = model_source.Model(path, asset).label
        try:
            glb = glb_of(path, asset)
        except model_source.ConversionError as error:
            self._warn(str(error))
            return None
        # Blender's importer would follow a uri anywhere; the engine refuses these, so the
        # viewport must not show what the game will never get.
        refusal = gltf.reference_refusal(glb)
        if refusal is not None:
            self._warn(f"could not import {label}: {refusal}")
            return None

        basename = os.path.basename(path)
        # Only a ``.glb`` is named by its stem: ``car.blend`` or ``car.gltf`` beside ``car.glb``
        # must not take over that model's collection.
        name = f"GLB/{os.path.splitext(basename)[0] if path.lower().endswith('.glb') else basename}"
        if asset is not None:
            name += f"/{asset}"
        stamp = "|".join(store.stamp_of(part) for part in (path, *gltf.buffer_files(path)))
        if glb != path:
            stamp += f"|{store.stamp_of(glb)}"
        existing = bpy.data.collections.get(name)
        if existing is not None and _is_current_import(existing, path, asset, stamp):
            return existing
        if existing is not None and _same_source(existing, path, asset):
            # Drop the stale collection, or the import lands on GLB/Foo.001 and leaks the old mesh.
            _discard_library_collection(existing)

        # The importer cannot be redirected; diff the tables, since names get suffixed. The
        # COLLECTIONS are diffed too because the glTF importer makes its own -- `glTF_not_exported`
        # on any file that has such nodes -- and links them to the scene, where they sat in the
        # Outliner beside the library for the life of the session.
        before = set(bpy.data.objects)
        collections_before = set(bpy.data.collections)
        try:
            bpy.ops.import_scene.gltf(filepath=glb)
        except RuntimeError as error:
            self._warn(f"could not import {label}: {error}")
            return None

        created = [obj for obj in bpy.data.objects if obj not in before]
        imported_collections = [
            found for found in bpy.data.collections if found not in collections_before
        ]
        if not created:
            self._warn(f"{label} imported nothing")
            return None

        collection = bpy.data.collections.new(name)
        collection[SOURCE_KEY] = os.path.abspath(path)
        if asset is not None:
            collection[ASSET_KEY] = asset
        collection[STAMP_KEY] = stamp
        self._root.children.link(collection)

        for obj in created:
            for parent in list(obj.users_collection):
                parent.objects.unlink(obj)
            collection.objects.link(obj)

        # Only the ones the move left EMPTY: a collection still holding something is structure
        # the GLB declared, and dropping it would take that something with it.
        for found in imported_collections:
            if not found.objects and not found.children:
                bpy.data.collections.remove(found)

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


def _is_current_import(collection: bpy.types.Collection, path: str, asset: str | None, stamp: str) -> bool:
    return _same_source(collection, path, asset) and collection.get(STAMP_KEY) == stamp


def _same_source(collection: bpy.types.Collection, path: str, asset: str | None) -> bool:
    if collection.get(ASSET_KEY) != asset:
        return False
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
