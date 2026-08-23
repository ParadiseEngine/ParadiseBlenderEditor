"""Viewport previews for authored model paths.

An entity whose ``model_path`` points at an existing GLB exports as a pure reference: the
exporter never reads the file, so in Blender the entity renders as an EMPTY and the author is
placing a box they cannot see. :class:`PARADISE_OT_load_model_preview` fills that gap by
importing the referenced glTF and attaching its geometry as a CHILD object of the entity.

The child-object shape is the load-bearing decision. Every consumer of scene data --
``export/mesh.py``, ``export/navmesh.py``, ``live/sync.py``, the GUID handler -- walks ENTITY
objects, and a preview child is not one, so a level exports identically whether or not
previews are loaded. (Attaching the mesh to the entity itself would also have worked --
``resolve_mesh_field`` lets an authored path win over geometry -- but the imported materials
would land in ``obj.material_slots``, and those slots are exported as the contract's
``Materials`` list: shipped data would change because someone wanted to look at it.)

Three simplifications, all deliberate:

* **Geometry only.** Authored GLBs under ``data/`` carry KTX2 textures -- the engine's reader
  rejects PNG/JPEG, so anything that ships has them -- and Blender's importer cannot read
  KTX2. Rather than pretend otherwise, the preview strips materials: a grey silhouette that
  is honestly the right shape beats one that claims to be the asset.
* **Lossy transforms.** Detaching imported objects from their parents goes through a
  ``matrix_world`` round trip, lossy at ~1e-6 -- exactly what ``export/mesh.py`` goes to
  lengths to avoid. This mesh is never exported, so nothing keys on its content.
* **Manual refresh.** Loading again rebuilds a preview whose file changed on disk (mtime
  recorded at import is compared), but nothing watches files live.
"""

from __future__ import annotations

import contextlib
import os

import bpy
from bpy.types import Operator
from mathutils import Matrix

from .. import log
from .entity import entity_objects, is_entity

__all__ = ["CHILD_KEY", "classes", "has_preview", "scene_has_previews", "scene_preview_entities"]

#: Absolute source path, stored as an ID property on a preview mesh datablock. Presence of the
#: key IS the marker that separates preview meshes from authored geometry -- nothing else
#: distinguishes them, and everything that must ignore previews checks it.
SOURCE_KEY = "paradise_preview_source"

#: The source file's mtime at import time, compared on reload to decide staleness.
MTIME_KEY = "paradise_preview_mtime"

#: Marker on the child OBJECT a preview is attached through.
CHILD_KEY = "paradise_preview_child"

#: Datablock-name prefix, so previews are recognisable in the outliner's data view.
PREVIEW_PREFIX = "ParadisePreview/"


def has_preview(entity: bpy.types.Object) -> bool:
    """Whether the entity currently displays a model preview child."""
    return any(child.get(CHILD_KEY) for child in entity.children)


def scene_has_previews(context) -> bool:
    """Whether any preview child is linked into the current scene.

    Scene-scoped rather than entity-scoped on purpose: deleting an ENTITY in Blender leaves
    its preview child behind as an ordinary root object, and the panel's Unload must still
    see it.
    """
    return any(obj.get(CHILD_KEY) for obj in context.scene.objects)


def _purge_orphan_preview_meshes() -> int:
    """Remove preview datablocks nobody displays; returns how many went."""
    orphans = [m for m in bpy.data.meshes if m.get(SOURCE_KEY) and m.users == 0]
    for mesh in orphans:
        bpy.data.meshes.remove(mesh)
    return len(orphans)


# --------------------------------------------------------------------------------------
# Building the preview mesh
# --------------------------------------------------------------------------------------


def _find_mesh(absolute: str) -> bpy.types.Mesh | None:
    """The preview mesh already built for this path, if any."""
    for mesh in bpy.data.meshes:
        if mesh.get(SOURCE_KEY) == absolute:
            return mesh
    return None


def _preview_mesh(absolute: str) -> bpy.types.Mesh | None:
    """A preview mesh for ``absolute``, reusing or refreshing what already exists.

    Multiple entities referencing the same GLB share one datablock -- the same deduplication
    the mesh exporter applies to authored geometry. A file that changed since import is
    rebuilt and swapped into every child still holding the stale copy.
    """
    existing = _find_mesh(absolute)
    if existing is not None and float(existing.get(MTIME_KEY, 0.0)) >= os.path.getmtime(absolute):
        return existing

    mesh = _build_mesh(absolute)
    if mesh is None or existing is None or existing == mesh:
        return mesh

    for obj in bpy.data.objects:
        if obj.type == "MESH" and obj.data == existing and obj.get(CHILD_KEY):
            obj.data = mesh
    if existing.users == 0:
        bpy.data.meshes.remove(existing)
    return mesh


def _build_mesh(absolute: str) -> bpy.types.Mesh | None:
    """Import one glTF file and collapse it to a single transform-free mesh datablock.

    The result's vertices are in the imported scene's world space, which is entity-local
    space: the runtime renders a GLB's contents at the entity's ``WorldMatrix`` unchanged,
    and Blender's importer applies the same Y-up basis change its exporter does. A child at
    identity local transform therefore displays exactly where the runtime would.
    """
    existing_objects = {o.name for o in bpy.data.objects}
    existing_meshes = {m.name for m in bpy.data.meshes}
    existing_materials = {m.name for m in bpy.data.materials}
    existing_images = {i.name for i in bpy.data.images}
    existing_actions = {a.name for a in bpy.data.actions}
    imported_names: list[str] = []
    imported_meshes: set[str] = set()
    imported_materials: set[str] = set()
    imported_images: set[str] = set()
    imported_actions: set[str] = set()
    mesh: bpy.types.Mesh | None = None

    view_layer = bpy.context.view_layer
    saved_selection = list(bpy.context.selected_objects)
    saved_active = view_layer.objects.active

    try:
        bpy.ops.import_scene.gltf(
            filepath=absolute,
            import_pack_images=False,  # KTX2 cannot be packed; copy nothing it cannot read
            # NOTE: 5.2's importer has no "skip animations" switch, so a file with clips
            # imports them as actions on armature objects. Those objects are deleted below
            # and their 0-user actions purged in the finally -- a preview is a statue.
        )
        # Iterate the datablocks themselves: iterating a bpy_prop_collection yields
        # OBJECTS, while .keys() yields names -- the distinction the lint rule cannot see.
        imported_names = [o.name for o in bpy.data.objects if o.name not in existing_objects]
        # Datablock names the import created, as the COMPLEMENT of the before-sets: the
        # importer suffixes on collision (a leftover 0-user "Cube" makes the new one
        # "Cube.001"), so a name captured naively could belong to the author's own data.
        imported_meshes = {m.name for m in bpy.data.meshes} - existing_meshes
        imported_materials = {m.name for m in bpy.data.materials} - existing_materials
        imported_images = {i.name for i in bpy.data.images} - existing_images
        imported_actions = {a.name for a in bpy.data.actions} - existing_actions
        # Re-resolve by name after every bpy.ops call: join() deletes what it merges, and a
        # Python reference held across that is a removed StructRNA (see import_car.py).
        meshes = [
            bpy.data.objects[n] for n in imported_names
            if bpy.data.objects[n].type == "MESH" and bpy.data.objects[n].data is not None
        ]
        if not meshes:
            log.error(f"'{absolute}' contains no meshes, so there is nothing to preview.")
            return None

        bpy.ops.object.select_all(action="DESELECT")
        for obj in meshes:
            # A hidden object cannot be selected, and transform_apply would then miss it.
            obj.hide_set(False)
            obj.select_set(True)
        view_layer.objects.active = meshes[0]

        for obj in meshes:
            # Morph targets arrive as shape keys: they block transform_apply and mean
            # nothing to a static preview.
            if obj.data.shape_keys is not None:
                obj.shape_key_clear()
            # A multi-user mesh (the importer deduplicates instanced nodes) cannot be
            # transformed in place; give this object its own copy.
            if obj.data.users > 1:
                obj.data = obj.data.copy()
            if obj.parent is not None:
                # Detach from the imported hierarchy KEEPING world placement, so the bake
                # below writes each part where the file put it. The matrix_world round trip
                # is lossy at ~1e-6 -- acceptable only because this mesh is never exported.
                world = obj.matrix_world.copy()
                obj.parent = None
                obj.matrix_world = world

        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
        if len(meshes) > 1:
            bpy.ops.object.join()
        mesh = view_layer.objects.active.data

        # Materials are stripped on purpose: an object's slots become the contract's
        # Materials list, and the imported ones reference KTX2 images Blender cannot read.
        mesh.materials.clear()

        mesh[SOURCE_KEY] = absolute
        mesh[MTIME_KEY] = os.path.getmtime(absolute)
        stem = os.path.splitext(os.path.basename(absolute))[0]
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in stem).strip("._") or "model"
        mesh.name = f"{PREVIEW_PREFIX}{safe}"
        return mesh
    except RuntimeError as error:
        log.error(f"Could not import '{absolute}' for preview: {error}")
        return None
    finally:
        bpy.ops.object.select_all(action="DESELECT")
        for name in imported_names:
            orphan = bpy.data.objects.get(name)
            if orphan is not None:
                bpy.data.objects.remove(orphan, do_unlink=True)
        # Drop what the import created and nothing else -- a blanket purge of zero-user
        # datablocks would eat the author's own unpacked-but-unused assets. The kept mesh
        # carries SOURCE_KEY and is exempt even though the child attaching to it is the
        # caller's next step, not this function's.
        for collection, added in (
            (bpy.data.meshes, imported_meshes),
            (bpy.data.materials, imported_materials),
            (bpy.data.images, imported_images),
            (bpy.data.actions, imported_actions),
        ):
            for name in added:
                block = collection.get(name)
                if block is not None and block.users == 0 and not block.get(SOURCE_KEY):
                    collection.remove(block)
        for previously in saved_selection:
            with contextlib.suppress(RuntimeError):
                previously.select_set(True)
        view_layer.objects.active = saved_active


def _attach_child(entity: bpy.types.Object, mesh: bpy.types.Mesh) -> None:
    """Point the entity's preview child at ``mesh``, creating it on first load.

    Refresh reuses the existing child and only swaps the datablock -- recreating the object
    every load would churn identity (and any viewport state riding on it) for nothing.
    """
    existing = next((c for c in entity.children if c.get(CHILD_KEY)), None)
    if existing is not None:
        existing.data = mesh
        return

    child = bpy.data.objects.new(f"{entity.name}.preview", mesh)
    entity.users_collection[0].objects.link(child)
    child.parent = entity
    child.matrix_local = Matrix.Identity(4)
    child.matrix_parent_inverse = Matrix.Identity(4)
    child[CHILD_KEY] = True
    # A viewport aid, not scene content: keep it out of final renders.
    child.hide_render = True


# --------------------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------------------


def _resolve_source(
    entity: bpy.types.Object, warned: set[str] | None = None
) -> str | None:
    """Absolute path of the entity's authored model, or ``None`` with a warning logged.

    ``warned`` suppresses repeats when a whole scene shares one broken reference -- a
    scene-wide load of twenty entities pointing at one missing file should say so once.
    """
    seen = warned if warned is not None else set()
    authored = entity.paradise.model_path.strip()
    if not authored.lower().endswith((".glb", ".gltf")):
        if authored not in seen:
            seen.add(authored)
            log.warn(
                f"Entity '{entity.name}' has model path '{authored}', which is not a "
                ".glb/.gltf; the runtime only loads glTF, so there is nothing to preview."
            )
        return None
    absolute = os.path.abspath(bpy.path.abspath(authored))
    if not os.path.exists(absolute):
        if absolute not in seen:
            seen.add(absolute)
            log.error(
                f"Entity '{entity.name}' references '{authored}', which does not exist on "
                "disk; no preview was created. The export will carry the reference anyway, "
                "and the runtime will fail to load it."
            )
        return None
    return absolute


def _entities_to_preview(context) -> list[bpy.types.Object]:
    """Selected entities with an authored model path (the active one when none is selected)."""
    objects = context.selected_objects or (
        [context.active_object] if context.active_object is not None else []
    )
    return [o for o in objects if is_entity(o) and o.paradise.model_path.strip()]


def scene_preview_entities(scene) -> list[bpy.types.Object]:
    """Every entity in the scene with an authored model path."""
    return [o for o in entity_objects(scene) if o.paradise.model_path.strip()]


def _load_preview(entity: bpy.types.Object, warned: set[str]) -> bool:
    """Load or refresh one entity's preview; the shared body of both load operators."""
    absolute = _resolve_source(entity, warned)
    if absolute is None:
        return False
    mesh = _preview_mesh(absolute)
    if mesh is None:
        return False
    _attach_child(entity, mesh)
    return True


class PARADISE_OT_load_model_preview(Operator):
    """Show the model referenced by Model Path as viewport geometry on this entity"""

    bl_idname = "paradise.load_model_preview"
    bl_label = "Load Model Preview"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context) -> bool:
        # The import/join chain below is bpy.ops, which only run in object mode.
        return context.mode == "OBJECT" and bool(_entities_to_preview(context))

    def execute(self, context):
        warned: set[str] = set()
        loaded = sum(1 for e in _entities_to_preview(context) if _load_preview(e, warned))

        if not loaded:
            return {"CANCELLED"}
        log.info(f"Loaded model preview for {loaded} entity(ies); geometry only -- Blender "
                 "cannot read the KTX2 textures the runtime requires.", self)
        return {"FINISHED"}


class PARADISE_OT_load_model_previews_scene(Operator):
    """Load or refresh viewport previews for every entity with a Model path in this scene"""

    bl_idname = "paradise.load_model_previews_scene"
    bl_label = "Load Model Previews (Scene)"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context) -> bool:
        # The import/join chain below is bpy.ops, which only run in object mode.
        return (
            context.mode == "OBJECT"
            and bool(scene_preview_entities(context.scene))
        )

    def execute(self, context):
        warned: set[str] = set()
        targets = scene_preview_entities(context.scene)
        loaded = sum(1 for e in targets if _load_preview(e, warned))

        if not loaded:
            return {"CANCELLED"}
        # Clicking again IS the refresh: an unchanged file reuses its datablock (mtime check
        # in _preview_mesh), a changed one is rebuilt and swapped into its children.
        log.info(
            f"Previews loaded for {loaded}/{len(targets)} referenced model entit(ies); "
            "geometry only -- Blender cannot read the KTX2 textures the runtime requires.",
            self,
        )
        return {"FINISHED"}


class PARADISE_OT_clear_model_preview(Operator):
    """Remove the viewport preview from the selected entities"""

    bl_idname = "paradise.clear_model_preview"
    bl_label = "Clear Model Preview"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context) -> bool:
        objects = context.selected_objects or (
            [context.active_object] if context.active_object is not None else []
        )
        return any(is_entity(o) and has_preview(o) for o in objects)

    def execute(self, context):
        cleared = 0
        for entity in [o for o in context.selected_objects or [context.active_object]
                       if o is not None and is_entity(o) and has_preview(o)]:
            for child in [c for c in entity.children if c.get(CHILD_KEY)]:
                bpy.data.objects.remove(child, do_unlink=True)
            cleared += 1

        purged = _purge_orphan_preview_meshes()
        if not cleared:
            return {"CANCELLED"}
        log.info(f"Cleared model preview from {cleared} entity(ies), {purged} mesh(es) released.",
                 self)
        return {"FINISHED"}


class PARADISE_OT_clear_model_previews_scene(Operator):
    """Remove every model preview from this scene, including ones whose entity is gone"""

    bl_idname = "paradise.clear_model_previews_scene"
    bl_label = "Unload Model Previews (Scene)"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context) -> bool:
        return scene_has_previews(context)

    def execute(self, context):
        # Every object marked as a preview child, not just the ones still parented to a live
        # entity: deleting an entity orphans its preview as a root object, and this sweep is
        # the only thing that still recognises it. Snapshot first: removing while iterating
        # a bpy_prop_collection is undefined.
        targets = [o for o in context.scene.objects if o.get(CHILD_KEY)]
        for obj in targets:
            bpy.data.objects.remove(obj, do_unlink=True)
        removed = len(targets)

        purged = _purge_orphan_preview_meshes()
        if not removed:
            return {"CANCELLED"}
        log.info(f"Unloaded {removed} model preview(s), {purged} mesh(es) released.", self)
        return {"FINISHED"}


classes = (
    PARADISE_OT_load_model_preview,
    PARADISE_OT_clear_model_preview,
    PARADISE_OT_load_model_previews_scene,
    PARADISE_OT_clear_model_previews_scene,
)
