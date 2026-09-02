"""Schema-driven authored components on Blender objects (the Godot host's ``AuthoredEntityCore``).

Storage is ID properties, not a ``PropertyGroup``: a property group's fields are class-level and
registered once, but the schema changes on every game rebuild, mid-session. Keys are
``obj["paradise_components"]`` (enabled ids, in order added) and
``obj["paradise:<token>/<Field/Path>"]`` (one value per flattened field, the Godot host's
``_values`` keying). Values are stored at schema type; enums as the member NAME the contract serializes.
"""

from __future__ import annotations

import base64
import math
import uuid
from dataclasses import dataclass

import bpy
from bpy.types import Operator

from .. import log
from ..contract import authoring, component_ids, well_known
from ..contract.authoring import schema_for_data_dir, schema_load_error
from ..prefs import resolve_blender_data_dir

__all__ = [
    "apply_ui_metadata",
    "bake_camera_refs",
    "bake_leaf_refs",
    "bake_light_refs",
    "bake_self_refs",
    "bake_shape_refs",
    "bake_transform_refs",
    "build_component_payloads",
    "classes",
    "enabled_component_ids",
    "has_component",
    "host_entries",
    "host_list_field",
    "host_ref_key",
    "is_host_list",
    "is_present",
    "key_token",
    "schema_for_data_dir",
    "schema_load_error",
    "storage_value",
    "value_key",
    "value_key_prefix",
    "values_for",
]

ENABLED_KEY = "paradise_components"
VALUE_PREFIX = "paradise:"

def schema_for(context) -> authoring.AuthoringSchemaDocument:
    return schema_for_data_dir(resolve_blender_data_dir(context.scene))


def component_by_id(
    document: authoring.AuthoringSchemaDocument, component_id: str
) -> authoring.AuthoredComponentSchema | None:
    for component in document.components:
        if component.id == component_id:
            return component
    return None


def enabled_component_ids(obj: bpy.types.Object) -> list[str]:
    stored = obj.get(ENABLED_KEY)
    if stored is None:
        return []
    return [str(component_id) for component_id in stored]


MAX_KEY_LENGTH = 63


def key_token(component_id: str) -> str:
    """A component id compacted for use inside an ID-property name.

    Blender caps a property name at 63 characters; a 36-char GUID would leave 17 for the field
    path, less than ``FollowYawSmoothingSeconds`` alone. Base64 of the 16 bytes is 22, leaving 31.
    base64url because ``/`` separates the id from the path in every key. Storage naming only:
    the payload carries the canonical GUID, so the byte order (RFC 4122, not .NET's
    ``Guid.ToByteArray``) only has to agree with itself.
    """
    try:
        raw = uuid.UUID(component_id).bytes
    except (ValueError, AttributeError, TypeError):
        return component_id
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def value_key_prefix(component_id: str) -> str:
    """The prefix every key of one component shares; scans must use this, not the raw id."""
    return f"{VALUE_PREFIX}{key_token(component_id)}/"


def value_key(component_id: str, path: str) -> str:
    return f"{value_key_prefix(component_id)}{path}"


def has_component(obj: bpy.types.Object, component_id: str) -> bool:
    return component_id in enabled_component_ids(obj)


def stored_value(obj: bpy.types.Object, component_id: str, path: str, default=None):
    return obj.get(value_key(component_id, path), default)


#: Components derived from Blender data (mesh pipeline, lamps, Placement). Shown as read-only
#: rows so they never grow a second, disagreeing form.
HOST_DERIVED_IDS = frozenset({
    component_ids.RENDERABLE,
    component_ids.LIGHT,
    component_ids.SPRITE_ANIMATION,
    well_known.META_ID,
    well_known.TRANSFORM_ID,
    component_ids.MATERIALS,
})

def host_list_field(component: authoring.AuthoredComponentSchema):
    """The array field whose items are authored by pointing at host objects, or None."""
    for field in component.fields:
        items = field.items
        if field.type == authoring.TYPE_ARRAY and items is not None and items.authored_by:
            return field
    return None


def is_host_list(component: authoring.AuthoredComponentSchema) -> bool:
    """Whether the body is a host-object list. The schema decides, never a hand-written id map:
    that map made host references an engine privilege a game component could not claim."""
    return host_list_field(component) is not None


def is_authorable(component: authoring.AuthoredComponentSchema) -> bool:
    """Whether the panel offers this component. A type-level host kind this host implements is
    offered (the picker is the form); an unimplemented kind stays hidden rather than drawing a
    control that exports nothing."""
    if component.id in HOST_DERIVED_IDS:
        return False
    if component.authored_by is None:
        return True
    return component.authored_by in authoring.HOST_IMPLEMENTED_KINDS


def host_ref_key(component: authoring.AuthoredComponentSchema) -> str:
    """``<component-id>/<field-path>``: per field, so two host lists on one component stay apart."""
    field = host_list_field(component)
    return f"{component.id}/{field.name if field is not None else ''}"


@dataclass(frozen=True)
class HostEntry:
    target: object
    index: int
    key: str = ""


def host_entries(
    obj: bpy.types.Object, component: authoring.AuthoredComponentSchema
) -> list[HostEntry]:
    """This component's host-list references. Indices are absolute within the one shared store,
    so a filtered view still removes the row the panel pointed at."""
    props = getattr(obj, "paradise", None)
    if props is None:
        return []

    key = host_ref_key(component)
    return [
        HostEntry(target=item.target, index=index, key=key)
        for index, item in enumerate(getattr(props, "host_refs", []) or [])
        if item.key == key
    ]


def collider_entries(obj: bpy.types.Object, data_dir: str) -> list[HostEntry]:
    """The collider references, named because the exporter (static rigidbody) and the navmesh
    bake genuinely mean colliders; everything else goes through :func:`host_entries`."""
    component = component_by_id(schema_for_data_dir(data_dir), component_ids.COLLIDER)
    return host_entries(obj, component) if component is not None else []


def is_present(obj: bpy.types.Object, component: authoring.AuthoredComponentSchema) -> bool:
    """Present when enabled, or, for a host list, when it has entries: build scripts and older
    .blends fill the lists without touching the marker, and the export follows the lists."""
    if component.id in enabled_component_ids(obj):
        return True
    return bool(host_entries(obj, component))


def enable_component(obj: bpy.types.Object, component: authoring.AuthoredComponentSchema) -> None:
    enabled = enabled_component_ids(obj)
    if component.id not in enabled:
        obj[ENABLED_KEY] = [*enabled, component.id]

    if is_host_list(component):
        # ID properties here would be a second copy of data the referenced objects already are.
        return

    fields, _ = authoring.flatten(component)
    for field in fields:
        key = value_key(component.id, field.path)
        if len(key) > MAX_KEY_LENGTH:
            # Blender's own error names neither the key nor the fix (shorten it in the C#).
            log.warn(
                f"'{component.display_name}.{field.path}' does not fit Blender's "
                f"{MAX_KEY_LENGTH}-character property name limit ({len(key)} used). "
                "It is NOT authorable; shorten the field name."
            )
            continue
        if key in obj:
            continue  # re-enabling keeps previously authored values
        obj[key] = storage_value(field)
        apply_ui_metadata(obj, key, field)

    # References store the object's NAME, not an ID pointer: a dangling name is a diagnosable
    # export warning, a dangling pointer is a crash.
    for host in authoring.flatten(component)[1]:
        if not host.stores_slot:
            continue
        key = value_key(component.id, host.path)
        # Same guard as the field loop: unguarded, this write threw out of the Add button.
        if len(key) > MAX_KEY_LENGTH:
            log.warn(
                f"'{component.display_name}.{host.path}' does not fit Blender's "
                f"{MAX_KEY_LENGTH}-character property name limit ({len(key)} used). "
                "It is NOT authorable; shorten the field name."
            )
            continue
        if key not in obj:
            obj[key] = ""

    # A whole component authored by pointing at one host object (Godot's /Source picker).
    if (
        component.authored_by in authoring.HOST_RECORD_KINDS
        or component.authored_by in authoring.HOST_LEAF_KINDS
    ):
        key = value_key(component.id, authoring.HOST_SOURCE_PATH)
        if len(key) <= MAX_KEY_LENGTH and key not in obj:
            obj[key] = ""


def disable_component(obj: bpy.types.Object, component_id: str) -> None:
    enabled = enabled_component_ids(obj)
    if component_id in enabled:
        enabled.remove(component_id)
        if enabled:
            obj[ENABLED_KEY] = enabled
        else:
            del obj[ENABLED_KEY]

    prefix = value_key_prefix(component_id)
    # Snapshot the keys first: deleting while iterating an IDProperty group is undefined.
    for key in [key for key in obj.keys() if key.startswith(prefix)]:  # noqa: SIM118 -- IDPropertyGroup supports `in` only via .keys()
        del obj[key]

    # Leaving the references would keep exporting the component. Matched by key, not schema:
    # disable must work for a component the schema no longer declares.
    props = getattr(obj, "paradise", None)
    refs = getattr(props, "host_refs", None) if props is not None else None
    if refs is not None:
        prefix = f"{component_id}/"
        for index in reversed([i for i, item in enumerate(refs) if item.key.startswith(prefix)]):
            refs.remove(index)


def values_for(obj: bpy.types.Object, component: authoring.AuthoredComponentSchema) -> dict:
    values: dict[str, object] = {}
    for field in authoring.flatten(component)[0]:
        key = value_key(component.id, field.path)
        if key not in obj:
            continue
        stored = obj[key]
        # IDProperty arrays are not lists; the contract module expects plain Python.
        is_array = hasattr(stored, "__len__") and not isinstance(stored, str)
        values[field.path] = list(stored) if is_array else stored
    return values


def _resolve_slot(
    obj: bpy.types.Object,
    component: authoring.AuthoredComponentSchema,
    host: authoring.HostRef,
) -> bpy.types.Object | None:
    """The object a stored name points at, or None with a warning."""
    name = obj.get(value_key(component.id, host.path), "")
    if not isinstance(name, str) or not name:
        return None
    target = bpy.data.objects.get(name)
    if target is None:
        log.warn(
            f"'{obj.name}' points '{component.display_name}.{host.path}' at an object named "
            f"'{name}', which is not in this file. It is NOT exported, so the runtime sees "
            "the field unauthored — re-pick the object."
        )
    return target


def _apply_record(host: authoring.HostRef, baked: dict, values: dict) -> None:
    """Write the leaves the record declared, from a bake that offers every name it knows."""
    for leaf in host.bakes:
        if leaf in baked:
            values[f"{host.path}/{leaf}"] = baked[leaf]


def bake_self_refs(
    obj: bpy.types.Object,
    component: authoring.AuthoredComponentSchema,
    values: dict,
) -> dict:
    """Fill each self kind from THIS object. Never from the store: a stored copy of a name or
    pose can disagree with the object. Identity is :func:`.placement.identity`, the value
    ``meta.Guid`` carries; the parent is left for :class:`.placement.Placement` to settle, since
    only it knows which ancestor is exported."""
    from ..export.placement import ParentIdentity, identity
    from ..export.transform import decompose_contract

    for host in authoring.flatten(component)[1]:
        if not host.is_authorable or host.kind not in authoring.HOST_SELF_KINDS:
            continue
        if host.kind == authoring.HOST_ID:
            values[host.path] = identity(obj.name)
        elif host.kind == authoring.HOST_NAME:
            values[host.path] = obj.name
        elif host.kind == authoring.HOST_PARENT:
            values[host.path] = ParentIdentity()
        else:
            position, rotation, scale, _ = decompose_contract(obj.matrix_local)
            if host.kind == authoring.HOST_LOCAL_POSITION:
                values[host.path] = list(position)
            elif host.kind == authoring.HOST_LOCAL_ROTATION:
                values[host.path] = list(rotation)
            elif host.kind == authoring.HOST_LOCAL_SCALE:
                values[host.path] = list(scale)
    return values


def bake_leaf_refs(
    obj: bpy.types.Object,
    component: authoring.AuthoredComponentSchema,
    values: dict,
    paths=None,  # ExportPaths
    meshes=None,  # MeshExporter
) -> dict:
    """Fill each leaf reference (mesh, entity, sprite, asset) with the value it resolves to.

    A mesh slot pointing at itself is the normal case, so it does not warn like the pose bake.
    A dangling reference is left alone so the runtime can refuse an honest empty value rather
    than an invented one. ``meshes`` must be the export's own ``MeshExporter``: it dedupes by
    datablock, and a throwaway would re-export one GLB per object pointing at it.
    """
    from ..export.placement import identity

    for host in authoring.flatten(component)[1]:
        if not host.is_authorable or host.leaf_type is None:
            continue
        if host.kind in authoring.HOST_SELF_KINDS:
            continue
        stored = obj.get(value_key(component.id, host.path), "")
        if not isinstance(stored, str) or not stored:
            continue

        if host.kind == authoring.HOST_ASSET:
            values[host.path] = stored
            continue

        target = bpy.data.objects.get(stored)
        if target is None:
            log.warn(
                f"'{obj.name}' points '{component.display_name}.{host.path}' at an object named "
                f"'{stored}', which is not in this file. It is NOT exported, so the runtime sees "
                "the field unauthored — re-pick the object."
            )
            continue

        if host.kind == authoring.HOST_ENTITY:
            # Whether the target is exported depends on what IT authors, so a dangling identity
            # is the runtime's to refuse, where it can say so.
            values[host.path] = identity(target.name)
            continue

        if host.kind == authoring.HOST_SPRITE:
            if paths is None:
                continue
            field = _sprite_field(obj, target, paths)
            if field is None:
                continue
            values[host.path] = field
            continue

        if meshes is None or paths is None:
            continue
        field = meshes.resolve_mesh_field(target, paths)
        if field is None:
            log.warn(
                f"'{obj.name}' points '{component.display_name}.{host.path}' at '{target.name}', "
                "which has no geometry to export. It is NOT exported — point at a mesh object."
            )
            continue
        values[host.path] = field
    return values


def bake_transform_refs(
    obj: bpy.types.Object,
    component: authoring.AuthoredComponentSchema,
    values: dict,
) -> dict:
    """Fill each ``transform`` reference from where its referenced object stands.

    Rebased through the same ``decompose_contract`` every other transform uses; a second copy of
    the axis change is how two paths silently disagree about which way is up. ``Yaw`` is atan2
    over the rotated +Z, the runtime's own heading rule, not an Euler read. A dangling reference
    is warned and left alone: a silently zeroed destination is a real place.
    """
    # Local import: export.transform imports the contract package, so a top-level edge cycles.
    from ..export.transform import decompose_contract

    for host in authoring.flatten(component)[1]:
        if not host.is_authorable or host.kind != authoring.HOST_TRANSFORM:
            continue
        name = obj.get(value_key(component.id, host.path), "")
        if not isinstance(name, str) or not name:
            continue
        target = bpy.data.objects.get(name)
        if target is None:
            log.warn(
                f"'{obj.name}' points '{component.display_name}.{host.path}' at an object named "
                f"'{name}', which is not in this file. It is NOT exported, so the runtime sees "
                "the field unauthored — re-pick the object."
            )
            continue
        if target == obj:
            log.warn(
                f"'{obj.name}' points '{component.display_name}.{host.path}' at ITSELF. A "
                "reference to the volume it lives on carries no information; it is NOT exported."
            )
            continue

        position, rotation, scale, _ = decompose_contract(target.matrix_world)
        baked = {
            "Position": list(position),
            "Rotation": list(rotation),
            "Scale": list(scale),
            "Yaw": _yaw_of(rotation),
        }
        for leaf in host.bakes:
            values[f"{host.path}/{leaf}"] = baked[leaf]
    return values


def bake_shape_refs(
    obj: bpy.types.Object,
    component: authoring.AuthoredComponentSchema,
    values: dict,
) -> dict:
    """Fill each ``shape`` reference from the collider drawn on the object it points at, through
    the same ``export_shape`` the collider list uses so the two cannot disagree about a capsule."""
    from ..authoring.collider import is_collider
    from ..export.collider import export_shape

    for host in authoring.flatten(component)[1]:
        if not host.is_authorable or host.kind != authoring.HOST_SHAPE:
            continue
        name = obj.get(value_key(component.id, host.path), "")
        if not isinstance(name, str) or not name:
            continue
        target = bpy.data.objects.get(name)
        if target is None:
            log.warn(
                f"'{obj.name}' points '{component.display_name}.{host.path}' at an object named "
                f"'{name}', which is not in this file. It is NOT exported, so the runtime sees the "
                "field unauthored — re-pick the object."
            )
            continue
        if not is_collider(target):
            # Assigning an unmarked object would export a shape with no kind, which the runtime
            # reads as a box at the origin -- a volume in the wrong place rather than an absent one.
            log.warn(
                f"'{obj.name}' points '{component.display_name}.{host.path}' at '{target.name}', "
                "which is not marked as a Paradise collider. It is NOT exported — use Make Paradise "
                "Collider first."
            )
            continue

        shape = export_shape(obj, target)
        if shape is None:
            continue
        baked = shape.to_json()
        _apply_record(host, baked, values)
    return values


def bake_light_refs(
    obj: bpy.types.Object,
    component: authoring.AuthoredComponentSchema,
    values: dict,
) -> dict:
    """Fill each ``light`` reference from the lamp it points at."""
    from ..export.light import host_light_values

    for host in authoring.flatten(component)[1]:
        if not host.is_authorable or host.kind != authoring.HOST_LIGHT:
            continue
        target = _resolve_slot(obj, component, host)
        if target is None:
            continue
        baked = host_light_values(target)
        if baked is None:
            log.warn(
                f"'{obj.name}' points '{component.display_name}.{host.path}' at '{target.name}', "
                "which is not a lamp. It is NOT exported — point at a light object."
            )
            continue
        _apply_record(host, baked, values)
    return values


def bake_camera_refs(
    obj: bpy.types.Object,
    component: authoring.AuthoredComponentSchema,
    values: dict,
) -> dict:
    """Fill each ``camera`` reference from the Camera object it points at."""
    from ..export.camera import host_camera_values

    for host in authoring.flatten(component)[1]:
        if not host.is_authorable or host.kind != authoring.HOST_CAMERA:
            continue
        target = _resolve_slot(obj, component, host)
        if target is None:
            continue
        baked = host_camera_values(target)
        if baked is None:
            log.warn(
                f"'{obj.name}' points '{component.display_name}.{host.path}' at '{target.name}', "
                "which is not a camera. It is NOT exported — point at a camera object."
            )
            continue
        _apply_record(host, baked, values)
    return values


def bake_component_source(
    obj: bpy.types.Object,
    component: authoring.AuthoredComponentSchema,
    values: dict,
    paths=None,
    meshes=None,
) -> dict:
    """A type-level host kind: the Godot host's ``/Source`` merge, baked fields at the payload
    root; a leaf kind writes onto the component's first plain string field."""
    from ..export.placement import identity

    kind = component.authored_by
    if kind not in authoring.HOST_IMPLEMENTED_KINDS:
        return values
    if kind in authoring.HOST_SELF_KINDS:
        return values

    stored = obj.get(value_key(component.id, authoring.HOST_SOURCE_PATH), "")
    if not isinstance(stored, str) or not stored:
        return values

    if kind == authoring.HOST_ASSET:
        values.setdefault(_first_string_field(component) or authoring.HOST_SOURCE_PATH, stored)
        return values

    target = bpy.data.objects.get(stored)
    if target is None:
        log.warn(
            f"'{obj.name}' points '{component.display_name}' at an object named '{stored}', "
            "which is not in this file. It is NOT exported — re-pick the object."
        )
        return values

    baked: dict | str | None = None
    if kind == authoring.HOST_LIGHT:
        from ..export.light import host_light_values
        baked = host_light_values(target)
    elif kind == authoring.HOST_CAMERA:
        from ..export.camera import host_camera_values
        baked = host_camera_values(target)
    elif kind == authoring.HOST_TRANSFORM:
        from ..export.transform import decompose_contract
        position, rotation, scale, _ = decompose_contract(target.matrix_world)
        baked = {
            "Position": list(position),
            "Rotation": list(rotation),
            "Scale": list(scale),
            "Yaw": _yaw_of(rotation),
        }
    elif kind == authoring.HOST_SHAPE:
        from ..authoring.collider import is_collider
        from ..export.collider import export_shape
        if not is_collider(target):
            log.warn(
                f"'{obj.name}' points '{component.display_name}' at '{target.name}', "
                "which is not marked as a Paradise collider. It is NOT exported."
            )
            return values
        shape = export_shape(obj, target)
        baked = None if shape is None else shape.to_json()
    elif kind == authoring.HOST_ENTITY:
        baked = identity(target.name)
    elif kind == authoring.HOST_SPRITE:
        baked = _sprite_field(obj, target, paths) if paths is not None else None
    elif kind == authoring.HOST_MESH:
        if meshes is None or paths is None:
            return values
        baked = meshes.resolve_mesh_field(target, paths)

    if baked is None:
        return values
    if isinstance(baked, dict):
        for name, value in baked.items():
            values[name] = value
        return values
    field = _first_string_field(component)
    if field is not None:
        values[field] = baked
    return values


def _first_string_field(component: authoring.AuthoredComponentSchema) -> str | None:
    for field in component.fields:
        if field.type == authoring.TYPE_STRING and field.authored_by is None:
            return field.name
    return None


def _sprite_field(obj: bpy.types.Object, target: bpy.types.Object, paths) -> str | None:
    """Data-relative spritesheet field for an object that carries an image, or None."""
    image = _image_of(target)
    if image is None:
        log.warn(
            f"'{obj.name}' points a sprite reference at '{target.name}', which has no image. "
            "It is NOT exported — point at an image empty or a mesh with an image texture."
        )
        return None
    filepath = image.filepath
    if not filepath:
        log.warn(
            f"'{obj.name}' points a sprite reference at '{target.name}', whose image "
            f"'{image.name}' has no file. Pack the image to disk under data/sprites/."
        )
        return None
    absolute = bpy.path.abspath(filepath)
    field = paths.data_relative_field(absolute)
    if field is None or not field.replace("\\", "/").startswith("sprites/"):
        log.warn(
            f"'{obj.name}' points a sprite reference at '{target.name}', whose image "
            f"'{filepath}' is not under data/sprites/. It is NOT exported."
        )
        return None
    # The runtime reads KTX2 sidecars; the ingest pass writes them next to the source image.
    root, ext = field.rsplit(".", 1) if "." in field else (field, "")
    if ext.lower() in {"png", "jpg", "jpeg", "tga", "webp"}:
        field = f"{root}.ktx2"
    return field.replace("\\", "/")


def _image_of(obj: bpy.types.Object):
    data = getattr(obj, "data", None)
    if isinstance(data, bpy.types.Image):
        return data
    if obj.type == "MESH" and obj.data is not None:
        for slot in getattr(obj, "material_slots", []) or []:
            material = slot.material
            tree = getattr(material, "node_tree", None) if material is not None else None
            if tree is None:
                continue
            for node in tree.nodes:
                image = getattr(node, "image", None)
                if node.type == "TEX_IMAGE" and image is not None:
                    return image
    return None


def _yaw_of(rotation) -> float:
    """Yaw about +Y from a contract quaternion: atan2 over the rotated +Z, the same convention
    as the runtime's ``SceneBinding.HeadingOf``."""
    x, y, z, w = rotation
    forward_x = 2.0 * (x * z + w * y)
    forward_z = 1.0 - 2.0 * (x * x + y * y)
    return math.atan2(forward_x, forward_z)


def storage_value(field: authoring.FlatField, value=None):
    """A value coerced to the field's schema type. An ID property is typed by its first
    assignment and Blender keeps that type, so the coercion cannot be skipped; ``None`` lands on
    the default because an ID property cannot hold nothing."""
    default = field.default if value is None else value
    if field.type == authoring.TYPE_BOOL:
        return bool(default)
    if field.type == authoring.TYPE_INT:
        return int(default)
    if field.type == authoring.TYPE_FLOAT:
        return float(default)
    if field.type in (authoring.TYPE_STRING, authoring.TYPE_ENUM):
        return str(default)
    if field.type in (
        authoring.TYPE_VECTOR2,
        authoring.TYPE_VECTOR3,
        authoring.TYPE_QUATERNION,
        authoring.TYPE_COLOR,
    ):
        return [float(v) for v in default]
    return float(default) if isinstance(default, (int, float)) else 0.0


def apply_ui_metadata(store: bpy.types.ID, key: str, field: authoring.FlatField) -> None:
    """Attach doc/range/unit-subtype UI metadata to an ID property on any ID datablock."""
    try:
        ui = store.id_properties_ui(key)
    except TypeError:
        return  # strings and some array types carry no UI metadata; nothing to attach

    options: dict[str, object] = {}
    if field.doc:
        options["description"] = field.doc
    options.update(authoring.numeric_widget_options(field))
    if field.type == authoring.TYPE_COLOR:
        # COLOR_GAMMA, not COLOR: plain COLOR runs the floats through the view transform and
        # the Godot and Blender pickers would disagree about what "that orange" is.
        options["subtype"] = "COLOR_GAMMA"
        options["min"] = 0.0
        options["max"] = 1.0
    if field.type == authoring.TYPE_QUATERNION:
        options["subtype"] = "QUATERNION"

    if options:
        try:
            ui.update(**options)
        except Exception as error:  # metadata is cosmetic; never fail authoring over it
            log.warn(f"Could not apply UI metadata to '{key}': {error}")


def is_field_visible(obj: bpy.types.ID, component_id: str, field: authoring.FlatField) -> bool:
    """Honor ``visibleWhen``: the sibling is resolved within the same group, because the
    attribute references a property of the same record and a nested record is its own group."""
    condition = field.visible_when
    if condition is None:
        return True
    prefix = field.path[: field.path.rfind("/") + 1] if "/" in field.path else ""
    sibling = obj.get(value_key(component_id, prefix + condition.field))
    if sibling is None:
        return True  # an unresolvable condition shows the field rather than losing it
    expected = condition.equals
    if isinstance(expected, bool) or isinstance(sibling, bool):
        return bool(sibling) == bool(expected)
    if isinstance(expected, (int, float)) and isinstance(sibling, (int, float)):
        return float(sibling) == float(expected)
    return str(sibling) == str(expected)


def build_component_payloads(obj: bpy.types.Object, data_dir: str, paths=None, meshes=None) -> list:
    """Every enabled component as ``(id, type, payload)``, in schema order so two exports of an
    unchanged scene are identical. A component the current schema no longer declares is skipped
    with a warning: dropping authored values silently is the failure the warning names."""
    enabled = enabled_component_ids(obj)
    if not enabled:
        return []

    document = schema_for_data_dir(data_dir)
    pairs = []
    for component in document.components:  # already type-ordered by merge()
        if component.id not in enabled:
            continue
        if component.id == component_ids.COLLIDER:
            # Dedicated path because a collider list also derives a static rigidbody (entity.py).
            continue
        if is_host_list(component):
            payload = _host_list_payload(obj, component)
            if payload is not None:
                pairs.append((component.id, component.type, payload))
            continue
        if not is_authorable(component):
            log.warn(
                f"'{obj.name}' authors '{component.id}', which is not exportable from this "
                "host (an engine-reserved id, or a component authored only by a host-object "
                "bake). It is NOT exported."
            )
            continue
        values = bake_self_refs(obj, component, values_for(obj, component))
        values = bake_transform_refs(obj, component, values)
        values = bake_shape_refs(obj, component, values)
        values = bake_light_refs(obj, component, values)
        values = bake_camera_refs(obj, component, values)
        values = bake_leaf_refs(obj, component, values, paths, meshes)
        values = bake_component_source(obj, component, values, paths, meshes)
        payload = authoring.build_payload(component, values)
        pairs.append((component.id, component.type, payload))

    for component_id in enabled:
        if component_by_id(document, component_id) is None:
            log.warn(
                f"'{obj.name}' authors '{component_id}', which the current authoring schema "
                "does not declare. It is NOT exported. If the game renamed the component, "
                "re-add it; if the schema is stale, rebuild the game project."
            )
    return pairs


def _host_list_payload(
    obj: bpy.types.Object, component: authoring.AuthoredComponentSchema
) -> dict | None:
    """A host-list component's payload: plain fields plus the shapes its references bake to."""
    from ..export.collider import build_colliders

    field = host_list_field(component)
    if field is None:
        return None

    values = bake_self_refs(obj, component, values_for(obj, component))
    values = bake_transform_refs(obj, component, values)
    payload = authoring.build_payload(component, bake_shape_refs(obj, component, values))
    payload[field.name] = [
        shape.to_json() for shape in build_colliders(obj, host_entries(obj, component))
    ]
    return payload


# Blender's known enum-items gotcha: the callback's strings must stay referenced from Python
# or the UI reads freed memory. Module-level on purpose.
_enum_items_cache: list[tuple[str, str, str]] = []


def _addable_items(self, context):
    global _enum_items_cache
    obj = context.active_object
    document = schema_for(context)
    _enum_items_cache = [
        (component.id, component.display_name, component.id)
        for component in document.components
        if is_authorable(component) and obj is not None and not is_present(obj, component)
    ] or [("NONE", "(nothing to add)", "")]
    return _enum_items_cache


class PARADISE_OT_add_authored_component(Operator):
    """Attach a component from the game's authoring schema to this entity"""

    bl_idname = "paradise.add_authored_component"
    bl_label = "Add Component"
    bl_options = {"REGISTER", "UNDO"}
    bl_property = "component"

    component: bpy.props.EnumProperty(items=_addable_items)

    @classmethod
    def poll(cls, context) -> bool:
        return context.active_object is not None

    def invoke(self, context, _event):
        context.window_manager.invoke_search_popup(self)
        return {"FINISHED"}

    def execute(self, context):
        if self.component == "NONE":
            return {"CANCELLED"}
        component = component_by_id(schema_for(context), self.component)
        if component is None:
            self.report({"WARNING"}, f"'{self.component}' is not in the authoring schema.")
            return {"CANCELLED"}
        enable_component(context.active_object, component)
        # Search popups do not redraw the invoking panel on their own.
        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


class PARADISE_OT_remove_authored_component(Operator):
    """Remove this authored component and its values from the entity"""

    bl_idname = "paradise.remove_authored_component"
    bl_label = "Remove Component"
    bl_options = {"REGISTER", "UNDO"}

    component: bpy.props.StringProperty()

    def execute(self, context):
        disable_component(context.active_object, self.component)
        return {"FINISHED"}


def _enum_value_items(self, context):
    global _enum_items_cache
    component = component_by_id(schema_for(context), self.component)
    values: list[str] = []
    if component is not None:
        for field in authoring.flatten(component)[0]:
            if field.path == self.path and field.values:
                values = field.values
    _enum_items_cache = [(v, v, "") for v in values] or [("NONE", "(no values)", "")]
    return _enum_items_cache


class PARADISE_OT_build_game_schema(Operator):
    """Build the game project now, so its [Authored] components reach the schema"""

    bl_idname = "paradise.build_game_schema"
    bl_label = "Build Game Project"

    @classmethod
    def poll(cls, context) -> bool:
        from ..pipeline import schema_build

        return schema_build.resolved_project(context.scene) is not None

    def execute(self, context):
        from ..pipeline import schema_build

        project = schema_build.resolved_project(context.scene)
        if project is None:
            self.report({"WARNING"}, "Set the scene's Game Project first (Scene panel).")
            return {"CANCELLED"}
        if not schema_build.start_build(project, "requested from the panel"):
            self.report({"WARNING"}, "A build is already running, or no dotnet CLI was found.")
            return {"CANCELLED"}
        self.report({"INFO"}, "Building in the background; the schema hot-reloads when it lands.")
        return {"FINISHED"}


class PARADISE_OT_show_build_errors(Operator):
    """Show the last failed game build's compiler errors, and copy them to the clipboard"""

    bl_idname = "paradise.show_build_errors"
    bl_label = "Show Build Errors"

    @classmethod
    def poll(cls, context) -> bool:
        from ..pipeline import schema_build

        return bool(schema_build.last_failure())

    def invoke(self, context, _event):
        return context.window_manager.invoke_props_dialog(self, width=720)

    def draw(self, context) -> None:
        from ..pipeline import schema_build

        column = self.layout.column(align=True)
        for line in schema_build.last_failure():
            column.label(text=line[:160], icon="ERROR")
        if schema_build.last_failure_log():
            self.layout.separator()
            self.layout.label(text=f"Full output: {schema_build.last_failure_log()}")
        self.layout.label(text="OK copies the errors (and the log path) to the clipboard.")

    def execute(self, context):
        from ..pipeline import schema_build

        lines = list(schema_build.last_failure())
        if schema_build.last_failure_log():
            lines.append(f"full output: {schema_build.last_failure_log()}")
        context.window_manager.clipboard = "\n".join(lines)
        self.report({"INFO"}, "Build errors copied to the clipboard.")
        return {"FINISHED"}


class PARADISE_OT_sync_authored_component(Operator):
    """Create the ID properties for schema fields this component gained since it was enabled"""

    bl_idname = "paradise.sync_authored_component"
    bl_label = "Sync Component Fields"
    bl_options = {"REGISTER", "UNDO"}

    component: bpy.props.StringProperty()

    def execute(self, context):
        component = component_by_id(schema_for(context), self.component)
        if component is None:
            return {"CANCELLED"}
        enable_component(context.active_object, component)
        return {"FINISHED"}


class PARADISE_OT_set_authored_enum(Operator):
    """Choose a value for an enum field of an authored component"""

    bl_idname = "paradise.set_authored_enum"
    bl_label = "Set Value"
    bl_options = {"REGISTER", "UNDO"}
    bl_property = "value"

    component: bpy.props.StringProperty(options={"HIDDEN"})
    path: bpy.props.StringProperty(options={"HIDDEN"})
    value: bpy.props.EnumProperty(items=_enum_value_items)

    def invoke(self, context, _event):
        context.window_manager.invoke_search_popup(self)
        return {"FINISHED"}

    def execute(self, context):
        if self.value == "NONE":
            return {"CANCELLED"}
        context.active_object[value_key(self.component, self.path)] = self.value
        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


classes = [
    PARADISE_OT_add_authored_component,
    PARADISE_OT_build_game_schema,
    PARADISE_OT_show_build_errors,
    PARADISE_OT_remove_authored_component,
    PARADISE_OT_sync_authored_component,
    PARADISE_OT_set_authored_enum,
]
