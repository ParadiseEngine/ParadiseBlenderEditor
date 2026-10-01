"""Snapshot raw static meshes into a native .blend without editing their scene."""

from __future__ import annotations

import bpy
from mathutils import Matrix

from ..document.new_prefab import CreateError


def export(context, members: list, path: str, *, origin=None) -> None:
    """Write a static native snapshot, optionally about a caller's world-space origin."""
    graph = context.evaluated_depsgraph_get()
    if origin is None:
        active = context.active_object if context.active_object in members else members[0]
        origin = active.matrix_world.translation
    to_origin = Matrix.Translation(-origin)
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
            # Evaluated materials are runtime IDs, not serializable library dependencies.
            for index, slot in enumerate(evaluated.material_slots):
                if index < len(mesh.materials):
                    mesh.materials[index] = slot.material.original if slot.material else None
            obj = bpy.data.objects.new(source.name, mesh)
            objects.append(obj)
            temporary.collection.objects.link(obj)
            # Baking the complete world matrix retains shear from unselected scaled parents.
            transform = to_origin @ source.matrix_world
            mesh.transform(transform)
            if transform.determinant() < 0:
                mesh.flip_normals()

        # Keep the evaluated, transform-baked snapshot independent of unselected parents,
        # modifiers and the live scene. Writing this scene includes its material dependencies
        # without changing the current file, selection or invoking document-save handlers.
        # The staged file moves before publication, so relative dependencies must not point
        # back into its temporary directory. Remapping only affects the written file.
        # A newly populated scene's view layer must be synchronized before serialization;
        # Blender 5.2 can otherwise crash in libraries.write even for an empty new scene.
        temporary.view_layers[0].update()
        bpy.data.libraries.write(path, {temporary}, path_remap="ABSOLUTE", fake_user=False)
    finally:
        for obj in objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in meshes:
            bpy.data.meshes.remove(mesh)
        bpy.data.scenes.remove(temporary)
