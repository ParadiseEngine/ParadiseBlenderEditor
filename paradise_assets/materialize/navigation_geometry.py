"""Snapshot evaluated static document meshes, including GLB collection instances."""

from __future__ import annotations

from .. import edits
from ..document import component_schema
from . import store

_RIGIDBODY_ID = "b7ab4dd8-c8da-4dc2-9e5e-192fd74deb11"


def _owner(obj):
    while obj is not None:
        original = obj.original
        if store.guid_of(original):
            return original
        obj = obj.parent
    return None


def _moving(obj, vocabulary):
    while obj is not None:
        if obj.type == "ARMATURE" or obj.animation_data is not None:
            return True
        if obj.rigid_body is not None and obj.rigid_body.type == "ACTIVE":
            return True
        for component in store.component_json(obj):
            data = component.get("data", {})
            schema = vocabulary.describe(component)
            if schema is not None:
                for item in schema.plan(data):
                    # The displayed GLB may be a rigid placeholder for a skinned asset.
                    # Its authored mesh kind is authoritative even without a Blender rig.
                    if item.field.authored_by == "mesh":
                        kinds = {kind.lower() for kind in item.field.asset_kinds}
                        value = edits.read_path(data, item.path)
                        path = value.get("path", "") if isinstance(value, dict) else value
                        if kinds == {".skinnedmesh"} or (
                                isinstance(path, str) and path.lower().endswith(".skinnedmesh")):
                            return True
                    if item.field.authored_by == "navmesh-geometry":
                        value = edits.read_path(data, item.path)
                        if value is None:
                            value = item.field.default_value()
                        if value is False:
                            return True
            if str(component.get("id", "")).lower() == _RIGIDBODY_ID:
                if data.get("BodyType", "Dynamic") in ("Dynamic", "Kinematic"):
                    return True
        obj = obj.parent
    return False


def snapshot(scene):
    """World-space engine Y-up triangles; mirrored transforms retain outward winding."""
    layer = scene.view_layers[0]
    layer.update()
    layout = store.project_of(scene)
    vocabulary = (component_schema.load(layout.root) if layout is not None
                  else component_schema.Vocabulary({}, None))
    vertices, indices = [], []
    for instance in layer.depsgraph.object_instances:
        obj = instance.object
        if obj.type != "MESH" or not instance.show_self:
            continue
        owner = _owner(instance.parent if instance.is_instance else obj)
        if owner is None or _moving(owner, vocabulary) or _moving(obj.original, vocabulary):
            continue
        if any(mod.type == "ARMATURE" for mod in obj.modifiers):
            continue
        mesh = obj.to_mesh()
        try:
            mesh.calc_loop_triangles()
            offset = len(vertices)
            matrix = instance.matrix_world
            for vertex in mesh.vertices:
                x, y, z = matrix @ vertex.co
                vertices.append([x, z, -y])
            mirrored = matrix.to_3x3().determinant() < 0
            for triangle in mesh.loop_triangles:
                a, b, c = triangle.vertices
                indices.extend((offset + a, offset + (c if mirrored else b),
                                offset + (b if mirrored else c)))
        finally:
            obj.to_mesh_clear()
    if not indices:
        raise ValueError("No static document mesh geometry to bake")
    return {"vertices": vertices, "indices": indices}
