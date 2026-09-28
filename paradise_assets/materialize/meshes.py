"""Each referenced model loaded ONCE into a hidden library collection, instanced per object
(ShiningPie: ~117 files across 225 objects). Instancing also makes the geometry uneditable in
place, which is right: the model owns geometry, and an edit here would vanish on the next load.

A placement shows the model's SOURCE, loaded natively (``document/model_source.py``), so the level
shows quads, live modifiers, the source's own materials and its object hierarchy:

- A ``.blend`` is LINKED, read-only: an asset's collection, or for a whole-file model the objects
  of the file's scene. The asset collection is found by its ``paradise_guid``
  (``document/asset_guids.py``), not its name, so a collection renamed since the documents were
  extracted still shows. The library collection holds those linked objects and takes the asset
  collection's ``instance_offset``, so each asset places at its own origin, as the converter
  exports it.
- Any other source is imported by the importer call the converter makes
  (:func:`model_source.importer_for`); a ``.glb`` or ``.gltf`` by Blender's glTF importer.

Loading never converts anything: the converted GLB is what the engine cooks, and only what reads
the engine's structure reads it -- the clips (``document/glb_clips.py``).

The library keys and stamps each collection by the SOURCE (and the asset's GUID), its stamp
covering every file the load read: a ``.gltf``'s buffers, an ``.obj``'s ``.mtl``, the image files.
A moved stamp re-imports an imported model; a linked one has its ``Library`` reloaded -- every
model of that file follows at once -- and its collection's membership read again.

Materials are the source's own. A placement's ``Materials.Slots`` are not shown on them: only a
glTF import keeps wiring its untextured materials to the object colour, which the load sets from
``Slots[0]`` (:func:`tint_by_object_colour`); a linked material is read-only, and an imported
source's materials are shown as the source has them.
"""

from __future__ import annotations

import os
import struct

import blend_render_info  # Blender's own .blend header reader, in its scripts/modules
import bpy

from ..document import asset_guids, gltf, model_source
from . import store

__all__ = ["LIBRARY_COLLECTION", "MeshLibrary", "model_of"]

#: One collection per model, excluded from the view layer.
LIBRARY_COLLECTION = "ParadiseAssets/Library"

#: The model source the collection shows. The key -- like the ``GLB/`` collection names --
#: predates native sources; renaming it would orphan every existing workfile's library.
SOURCE_KEY = "paradise_glb_source"

#: The GUID of the asset of a multi-asset source the collection shows, and the asset
#: collection's name when it was linked; both absent for a whole-file model.
ASSET_KEY = "paradise_glb_asset"
ASSET_NAME_KEY = "paradise_glb_asset_name"

#: ``(mtime, size)`` of the source and each of its dependencies when it was loaded; a moved stamp
#: means load again. Also on a linked ``Library``, for when it was last read.
STAMP_KEY = "paradise_glb_stamp"

#: The files besides the source the load read, newline-separated, so the next load can tell
#: whether any moved without loading the model to find out.
DEPENDENCIES_KEY = "paradise_glb_dependencies"

#: What the converter's glTF export leaves out of a converted model (``export_cameras`` and
#: ``export_lights`` are off): shown, a model's lamp would light the level the game never lights.
_NOT_EXPORTED = frozenset({"CAMERA", "LIGHT", "LIGHT_PROBE"})


def model_of(collection: bpy.types.Collection | None) -> model_source.Model | None:
    """The model a library collection shows, or ``None`` for any other collection."""
    source = collection.get(SOURCE_KEY) if collection is not None else None
    if not isinstance(source, str):
        return None
    asset = asset_guids.canonical_of(collection.get(ASSET_KEY))
    name = collection.get(ASSET_NAME_KEY)
    return model_source.Model(source, asset, name if isinstance(name, str) and asset else None)


class MeshLibrary:
    """Loads models on demand and hands back a collection to instance."""

    def __init__(self, scene: bpy.types.Scene, warn=None) -> None:
        self._scene = scene
        self._warn = warn or (lambda message: None)
        self._by_path: dict[tuple[str, str | None], bpy.types.Collection | None] = {}
        self._root = _library_root(scene)

    @property
    def imported(self) -> int:
        """How many distinct models were loaded (a failed load does not count)."""
        return sum(1 for value in self._by_path.values() if value is not None)

    @property
    def sources(self) -> set[str]:
        """The model sources actually read, so a cache can key on what a load TOUCHED rather
        than a second reference-discovery that goes stale silently."""
        return {path for (path, _asset), value in self._by_path.items() if value is not None}

    def collection_for(self, model: model_source.Model) -> bpy.types.Collection | None:
        """The collection for ``model``, loading it on first use; ``None`` leaves the object an
        empty, since a placement whose mesh is missing is still authored data."""
        key = (os.path.normcase(os.path.abspath(model.path)), model.asset)
        if key in self._by_path:
            return self._by_path[key]

        collection = self._load(model)
        self._by_path[key] = collection
        return collection

    def _load(self, model: model_source.Model) -> bpy.types.Collection | None:
        path = model.path
        if not os.path.isfile(path):
            self._warn(f"mesh not found: {path}")
            return None
        basename = os.path.basename(path)
        # Only a ``.glb`` is named by its stem: ``car.blend`` or ``car.gltf`` beside ``car.glb``
        # must not take over that model's collection.
        name = f"GLB/{os.path.splitext(basename)[0] if path.lower().endswith('.glb') else basename}"
        if model.asset is not None:
            name += f"/{model.name or model.asset}"
        # By the tags, not the name: the name follows the asset collection's, which may change.
        existing = next((found for found in self._root.children
                         if found.library is None and _same_source(found, path, model.asset)), None)
        if model_source.is_linked(path):
            return self._link(model, name, existing)
        if model.asset is not None:
            self._warn(f"{model.label}: a {os.path.splitext(path)[1]} holds one model, not assets")
            return None
        return self._import(path, name, existing)

    def _link(self, model: model_source.Model, name: str,
              existing: bpy.types.Collection | None) -> bpy.types.Collection | None:
        """Link ``model`` from its ``.blend``; the library collection holds the linked objects
        themselves, so an edit saved in the source shows once it is reloaded."""
        path, asset, label = model.path, model.asset, model.label
        library = _library_of(path)
        if library is not None and library.get(STAMP_KEY) != _stamp(path, _stored_dependencies(library)):
            # Every placement of every model of the file follows: one reload re-reads them all.
            library.reload()
            _stamp_library(library, path)
        if (existing is not None and library is not None
                and existing.get(STAMP_KEY) == library.get(STAMP_KEY) and existing.all_objects):
            return existing

        try:
            # Only asset-marked collections are models of their own, as the converter reads it.
            with bpy.data.libraries.load(path, link=True, relative=True, assets_only=True) as (listed, _):
                assets = list(listed.collections)
            if asset is not None:
                container = _asset_collection(path, asset, model.name, assets)
                if container is None:
                    self._warn(f"{os.path.basename(path)} has no asset collection whose Paradise GUID is "
                               f"{asset}" + (f" (it was named '{model.name}')" if model.name else "")
                               + "; re-extract the file, or place one of its current assets")
                    return None
            elif assets:
                self._warn(f"{label} now holds asset collections; re-extract it and place one of "
                           "their prefabs")
                return None
            else:
                with bpy.data.libraries.load(path, link=True, relative=True) as (source, target):
                    if not source.scenes:
                        self._warn(f"{label} holds no scene to show")
                        return None
                    shown = _saved_scene(path, list(source.scenes))
                    if shown is None:
                        shown = sorted(source.scenes)[0]
                        self._warn(f"{label} holds {len(source.scenes)} scenes and does not say which it was "
                                   f"saved showing, the one the game gets: the level shows '{shown}'. Keep "
                                   "one scene in a model file")
                    target.scenes = [shown]
        except OSError as error:
            self._warn(f"could not link {label}: {error}")
            return None

        if asset is not None:
            members, offset = list(container.all_objects), container.instance_offset.copy()
        else:
            container = target.scenes[0]
            members, offset = list(container.collection.all_objects), None
        library = container.library
        # Again after every link: each model brings the images it uses, and the stamp must cover
        # them all for a texture saved alone to reload the file.
        _stamp_library(library, path)
        if asset is None:
            # Only its objects were wanted; a linked scene would sit in the scene switcher.
            bpy.data.scenes.remove(container)

        collection = existing if existing is not None else bpy.data.collections.new(name)
        _empty(collection)
        _tag(collection, path, asset, library.get(STAMP_KEY), _stored_dependencies(library))
        if asset is not None:
            collection[ASSET_NAME_KEY] = container.name
        if existing is None:
            self._root.children.link(collection)
        if offset is not None:
            collection.instance_offset = offset
        for obj in members:
            if obj.type not in _NOT_EXPORTED:
                collection.objects.link(obj)
        if not collection.objects:
            self._warn(f"{label} holds nothing to show")
        return collection

    def _import(self, path: str, name: str,
                existing: bpy.types.Collection | None) -> bpy.types.Collection | None:
        label = os.path.basename(path)
        importer = model_source.importer_for(path)
        if importer is None:
            self._warn(f"{label} is no model format Blender imports")
            return None
        is_gltf = importer == model_source.GLTF_IMPORTER
        if is_gltf:
            # Blender's importer would follow a uri anywhere; the engine refuses these, so the
            # viewport must not show what the game will never get.
            refusal = gltf.reference_refusal(path)
            if refusal is not None:
                self._warn(f"could not import {label}: {refusal}")
                return None

        if existing is not None:
            stamp = _stamp(path, _stored_dependencies(existing))
            if existing.get(STAMP_KEY) == stamp and existing.all_objects:
                return existing
            # Drop the stale collection, or the import lands on GLB/Foo.001 and leaks the old mesh.
            _discard_library_collection(existing)

        # The importers cannot be redirected; diff the tables, since names get suffixed. The
        # COLLECTIONS are diffed too because an importer may make its own -- the glTF one makes
        # `glTF_not_exported` on any file that has such nodes -- and links them to the scene,
        # where they sat in the Outliner beside the library for the life of the session.
        before = set(bpy.data.objects)
        collections_before = set(bpy.data.collections)
        images_before = set(bpy.data.images)
        scene = bpy.context.scene
        # The USD and Alembic importers set the scene's frame range to the file's by default;
        # the options must stay the converter's, so the level's range is put back instead.
        frames = (scene.frame_start, scene.frame_end, scene.render.fps, scene.render.fps_base)
        module, operator = importer.operator.split(".")
        try:
            getattr(getattr(bpy.ops, module), operator)(filepath=path, **dict(importer.options))
        except RuntimeError as error:
            self._warn(f"could not import {label}: {error}")
            return None
        finally:
            scene.frame_start, scene.frame_end, scene.render.fps, scene.render.fps_base = frames

        created = [obj for obj in bpy.data.objects if obj not in before]
        imported_collections = [
            found for found in bpy.data.collections if found not in collections_before
        ]
        if not is_gltf:
            for obj in [obj for obj in created if obj.type in _NOT_EXPORTED]:
                created.remove(obj)
                bpy.data.objects.remove(obj, do_unlink=True)
        if not created:
            self._warn(f"{label} imported nothing")
            return None

        images = [image for image in bpy.data.images if image not in images_before]
        dependencies = [*model_source.native_dependencies(path), *_image_files(images)]
        collection = bpy.data.collections.new(name)
        _tag(collection, path, None, _stamp(path, dependencies), dependencies)
        self._root.children.link(collection)

        for obj in created:
            for parent in list(obj.users_collection):
                parent.objects.unlink(obj)
            collection.objects.link(obj)

        # Only the ones the move left EMPTY: a collection still holding something is structure
        # the importer declared, and dropping it would take that something with it.
        for found in imported_collections:
            if not found.objects and not found.children:
                bpy.data.collections.remove(found)

        if is_gltf:
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


def _saved_scene(path: str, scenes: list[str]) -> str | None:
    """The scene of the ``.blend`` at ``path`` it was saved showing, among ``scenes``, or ``None``
    when the file cannot tell. The converter's glTF export marks that scene the default, and the
    engine reads the default. Linking cannot see it, but the render-info chunk Blender writes for
    background renders names it (with any scene flagged for background rendering, hence the
    uniqueness check)."""
    if len(scenes) == 1:
        return scenes[0]
    try:
        named = {name for _start, _end, name in blend_render_info.read_blend_rend_chunk(path)}
    except (OSError, ValueError, struct.error):
        return None
    candidates = [scene for scene in scenes if scene in named]
    return candidates[0] if len(candidates) == 1 else None


def _asset_collection(path: str, guid: str, hint: str | None,
                      names: list[str]) -> bpy.types.Collection | None:
    """The asset collection of the ``.blend`` at ``path`` whose ``paradise_guid`` is ``guid``,
    linked; ``names`` are the file's asset collections. Linking is by name and the GUID is inside
    the collection, so the name the document recorded (``hint``) is tried first -- the one link a
    current extraction needs -- and only when that is not it (the collection was renamed) is every
    other asset collection linked to find it. The first by name wins where two share the GUID,
    which the converter refuses anyway."""
    library = _library_of(path)
    linked = {
        found.name: found for found in bpy.data.collections
        if library is not None and found.library == library and not found.is_missing
    }
    first = [hint] if hint in names else []
    for batch in (first, sorted(name for name in names if name != hint)):
        wanted = [name for name in batch if name not in linked]
        if wanted:
            with bpy.data.libraries.load(path, link=True, relative=True) as (_source, target):
                target.collections = wanted
            linked.update((found.name, found) for found in target.collections if found is not None)
        for name in batch:
            found = linked.get(name)
            if found is not None and asset_guids.canonical_of(found.get(asset_guids.PROPERTY)) == guid:
                return found
    return None


def _library_of(path: str) -> bpy.types.Library | None:
    """The ``Library`` this file already links ``path`` through, if any."""
    wanted = os.path.normcase(os.path.abspath(path))
    for library in bpy.data.libraries:
        if library.parent is not None:
            continue
        if os.path.normcase(os.path.abspath(bpy.path.abspath(library.filepath))) == wanted:
            return library
    return None


def _stamp(path: str, dependencies) -> str:
    return "|".join(store.stamp_of(part) for part in (path, *dependencies))


def _stored_dependencies(block) -> list[str]:
    stored = block.get(DEPENDENCIES_KEY)
    return stored.split("\n") if isinstance(stored, str) and stored else []


def _stamp_library(library: bpy.types.Library, path: str) -> None:
    """Record what ``library`` was read from: the ``.blend`` and the image files its data names."""
    dependencies = _image_files(image for image in bpy.data.images if image.library == library)
    library[DEPENDENCIES_KEY] = "\n".join(dependencies)
    library[STAMP_KEY] = _stamp(path, dependencies)


def _image_files(images) -> list[str]:
    """The files ``images`` read, absolute; a packed or generated image reads none."""
    found = set()
    for image in images:
        if image.source in {"FILE", "SEQUENCE", "TILED"} and image.filepath and image.packed_file is None:
            found.add(os.path.normpath(bpy.path.abspath(image.filepath, library=image.library)))
    return sorted(found)


def _tag(collection: bpy.types.Collection, path: str, asset: str | None, stamp: str,
         dependencies: list[str]) -> None:
    collection[SOURCE_KEY] = os.path.abspath(path)
    if asset is not None:
        collection[ASSET_KEY] = asset
    else:
        for key in (ASSET_KEY, ASSET_NAME_KEY):
            if key in collection:
                del collection[key]
    collection[STAMP_KEY] = stamp
    collection[DEPENDENCIES_KEY] = "\n".join(dependencies)


def _same_source(collection: bpy.types.Collection, path: str, asset: str | None) -> bool:
    if asset_guids.canonical_of(collection.get(ASSET_KEY)) != asset:
        return False
    stored = collection.get(SOURCE_KEY)
    if not isinstance(stored, str) or not stored:
        return False
    return os.path.normcase(os.path.abspath(stored)) == os.path.normcase(os.path.abspath(path))


def _empty(collection: bpy.types.Collection) -> None:
    """Take everything out of ``collection``: an object this addon imported goes with it, a
    linked one stays its library's."""
    for child in list(collection.children):
        collection.children.unlink(child)
    for obj in list(collection.objects):
        if obj.library is None:
            bpy.data.objects.remove(obj, do_unlink=True)
        else:
            collection.objects.unlink(obj)


def _discard_library_collection(collection: bpy.types.Collection) -> None:
    _empty(collection)
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
