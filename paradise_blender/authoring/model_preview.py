"""Viewport previews for authored model paths, as a CHILD object of the entity.

A child, not the entity's own mesh: every exporter walks entity objects, so a level exports
identically with or without previews, and imported materials never reach ``material_slots``,
which the contract exports as ``Materials``. Materials are stripped because shipped GLBs carry
KTX2 that Blender cannot read; a grey silhouette beats a mesh that claims to be the asset. The
``matrix_world`` round trip here is lossy at ~1e-6, acceptable only because a preview is never
exported.
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

#: Presence of this key is the only thing that separates a preview mesh from authored geometry.
SOURCE_KEY = "paradise_preview_source"

MTIME_KEY = "paradise_preview_mtime"

CHILD_KEY = "paradise_preview_child"

PREVIEW_PREFIX = "ParadisePreview/"


def has_preview(entity: bpy.types.Object) -> bool:
    """Whether the entity currently displays a model preview child."""
    return any(child.get(CHILD_KEY) for child in entity.children)


def scene_has_previews(context) -> bool:
    """Scene-scoped: deleting an entity leaves its preview child behind as a root object."""
    return any(obj.get(CHILD_KEY) for obj in context.scene.objects)


def _purge_orphan_preview_meshes() -> int:
    """Remove preview datablocks nobody displays; returns how many went."""
    orphans = [m for m in bpy.data.meshes if m.get(SOURCE_KEY) and m.users == 0]
    for mesh in orphans:
        bpy.data.meshes.remove(mesh)
    return len(orphans)


def _find_mesh(absolute: str) -> bpy.types.Mesh | None:
    """The preview mesh already built for this path, if any."""
    for mesh in bpy.data.meshes:
        if mesh.get(SOURCE_KEY) == absolute:
            return mesh
    return None


def _preview_mesh(absolute: str) -> bpy.types.Mesh | None:
    """A preview mesh for ``absolute``, shared across entities and rebuilt when the file changed."""
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
    """Import a glTF and collapse it to one transform-free mesh. Vertices end up in entity-local
    space, so a child at identity displays exactly where the runtime would."""
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
            # 5.2's importer has no "skip animations" switch; actions are purged in the finally.
        )
        # A bpy_prop_collection iterates objects while .keys() yields names; lint cannot tell.
        imported_names = [o.name for o in bpy.data.objects if o.name not in existing_objects]
        # Complement of the before-sets: the importer suffixes on collision, so a captured
        # name could belong to the author's own data.
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
            # Shape keys block transform_apply.
            if obj.data.shape_keys is not None:
                obj.shape_key_clear()
            # A multi-user mesh cannot be transformed in place.
            if obj.data.users > 1:
                obj.data = obj.data.copy()
            if obj.parent is not None:
                # matrix_world assignment is lossy at ~1e-6: acceptable only because a
                # preview is never exported (export/mesh.py must never do this).
                world = obj.matrix_world.copy()
                obj.parent = None
                obj.matrix_world = world

        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
        if len(meshes) > 1:
            bpy.ops.object.join()
        mesh = view_layer.objects.active.data

        # Stripped: slots become the contract's Materials list (module docstring).
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
        # Only what the import created: a blanket zero-user purge would eat the author's own
        # unused assets.
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
    """Point the entity's preview child at ``mesh``, creating it on first load."""
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


def _resolve_source(
    entity: bpy.types.Object, warned: set[str] | None = None
) -> str | None:
    """Absolute path of the entity's authored model, or ``None`` with one warning per path."""
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


def _load_preview_guarded(entity: bpy.types.Object, warned: set[str]) -> bool:
    """:func:`_load_preview` with unexpected failures contained, so one malformed GLB does not
    abort a scene-wide load and silently skip every entity after it."""
    try:
        return _load_preview(entity, warned)
    except Exception as error:
        log.error(
            f"Preview for '{entity.name}' failed unexpectedly "
            f"({type(error).__name__}: {error}). Skipping this entity; the rest of the batch "
            "continues. The file may be malformed -- re-export it or open it in another "
            "glTF viewer to check."
        )
        return False


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
        loaded = sum(
            1 for e in _entities_to_preview(context) if _load_preview_guarded(e, warned)
        )

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
        loaded = sum(1 for e in targets if _load_preview_guarded(e, warned))

        if not loaded:
            return {"CANCELLED"}
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
        # Every marked child, including ones orphaned by a deleted entity. Snapshot first:
        # removing while iterating a bpy_prop_collection is undefined.
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
