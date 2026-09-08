"""Snapshot raw static meshes through Blender's glTF exporter, without editing their scene."""

from __future__ import annotations

import bpy
from mathutils import Matrix

from ..document.new_prefab import CreateError
from . import store


def selection(context) -> list:
    members = list(context.selected_objects)
    if context.mode != "OBJECT" or not members:
        raise CreateError("Select static mesh objects in Object Mode first.")
    for obj in members:
        if store.guid_of(obj) is not None:
            raise CreateError("Use Create Prefab from Object for existing Paradise document objects.")
        if obj.type != "MESH" or any(mod.type == "ARMATURE" for mod in obj.modifiers):
            raise CreateError(f"'{obj.name}' is not a static mesh. Select only unrigged mesh objects.")
    return members


def export(context, members: list, path: str) -> None:
    graph = context.evaluated_depsgraph_get()
    active = context.active_object if context.active_object in members else members[0]
    origin = Matrix.Translation(-active.matrix_world.translation)
    temporary = bpy.data.scenes.new("Paradise geometry snapshot")
    objects, meshes = [], []
    try:
        for source in members:
            evaluated = source.evaluated_get(graph)
            mesh = bpy.data.meshes.new_from_object(evaluated, depsgraph=graph)
            meshes.append(mesh)
            if not mesh.polygons:
                raise CreateError(f"'{source.name}' has no faces to save as geometry.")
            # Object-linked slots override the mesh datablock's material, including on two
            # objects sharing geometry. new_from_object alone keeps only the datablock side.
            for index, slot in enumerate(evaluated.material_slots):
                if index < len(mesh.materials):
                    mesh.materials[index] = slot.material
            obj = bpy.data.objects.new(source.name, mesh)
            objects.append(obj)
            temporary.collection.objects.link(obj)
            transform = origin @ source.matrix_world
            mesh.transform(transform)
            if transform.determinant() < 0:
                mesh.flip_normals()

        # Bake world matrices into vertices: a rotated child under nonuniform parent scale has
        # shear, which glTF node TRS cannot retain. Copies also cannot export unselected parents.
        with context.temp_override(scene=temporary, view_layer=temporary.view_layers[0]):
            result = bpy.ops.export_scene.gltf(
                filepath=path, export_format="GLB", use_active_scene=True,
                export_yup=True, export_animations=False, export_skins=False,
                export_cameras=False, export_lights=False,
            )
        if result != {"FINISHED"}:
            raise CreateError("Blender could not export the selected geometry.")
    finally:
        for obj in objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in meshes:
            bpy.data.meshes.remove(mesh)
        bpy.data.scenes.remove(temporary)
