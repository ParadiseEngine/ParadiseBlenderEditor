"""Schema-driven authored components on Blender objects.

The Blender half of what ``AuthoredEntityCore`` does in the Godot host: read the game's
``<data>/authoring-schema.json`` (dumped by the engine's Roslyn generator on every game
build), let an author attach the components it describes to an entity, and hand the values to
the exporter, which writes them into ``Components.Custom``.

Storage is Blender **ID properties**, not a ``PropertyGroup``. A property group's fields are
class-level and registered once, but the schema is data that changes on every game rebuild --
mid-session, with this addon already registered. ID properties are per-object and dynamic, so
a schema change needs no re-registration; the trade is that this module owns naming and
defaults itself:

* ``obj["paradise_components"]`` -- the enabled component ids, in the order they were added.
* ``obj["paradise:<component-id>/<Field/Path>"]`` -- one value per flattened schema field,
  mirroring the Godot host's ``_values`` keying so the two hosts read identically.

Values are stored at their schema type (a real bool for a bool -- ID properties support that
since Blender 3.0, and the manifest requires 4.2). Enums are stored as the member NAME string,
exactly what the contract serializes, so no mapping table is needed on either side.

The schema is re-read whenever the file's (mtime, size) stamp moves -- checked from panel
draws, which is the Blender analogue of the Godot host latching a stamp and re-loading when a
rebuild re-dumps the file.
"""

from __future__ import annotations

import base64
import math
import uuid
from dataclasses import dataclass

import bpy
from bpy.types import Operator

from .. import log
from ..contract import authoring, component_ids

# Both live in the contract layer now (component_ids has to read the schema during
# export, and contract/ may not import anything that imports bpy). Re-exported so
# every caller of authored_components keeps its spelling.
from ..contract.authoring import schema_for_data_dir, schema_load_error
from ..prefs import resolve_blender_data_dir

__all__ = [
    "apply_ui_metadata",
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


# --------------------------------------------------------------------------------------
# Per-object storage
# --------------------------------------------------------------------------------------


def enabled_component_ids(obj: bpy.types.Object) -> list[str]:
    stored = obj.get(ENABLED_KEY)
    if stored is None:
        return []
    return [str(component_id) for component_id in stored]


#: Blender's limit on an ID property NAME. The same cap :mod:`.config_store` enforces, restated
#: here because this module builds the other half of the keys.
MAX_KEY_LENGTH = 63


def key_token(component_id: str) -> str:
    """A component id compacted for use INSIDE an ID-property name.

    A canonical GUID is 36 characters, and Blender caps a property name at
    :data:`MAX_KEY_LENGTH`. Spent verbatim, an id would leave 17 characters for the field path --
    less than ShiningPie's ``FollowYawSmoothingSeconds`` alone. Base64 of the same 16 bytes is 22,
    which leaves 31.

    **base64url, not base64**: the alphabet must not contain ``/``, which separates the id from
    the field path in every key built from this.

    This token never leaves the .blend -- it is storage naming, not contract. The exported
    payload carries the full canonical GUID, so the byte order here only has to be consistent
    with itself (``uuid.UUID.bytes`` is RFC 4122 order, NOT .NET's ``Guid.ToByteArray``).

    A non-GUID id (a malformed schema, a hand-written test double) is returned unchanged, so the
    caller's length check reports the id a human actually wrote rather than a token of it.
    """
    try:
        raw = uuid.UUID(component_id).bytes
    except (ValueError, AttributeError, TypeError):
        return component_id
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def value_key_prefix(component_id: str) -> str:
    """Everything one component's keys share. Defined once so a scan for them cannot fall out of
    step with :func:`value_key` -- which it silently did the moment the id stopped being the
    literal thing inside the key."""
    return f"{VALUE_PREFIX}{key_token(component_id)}/"


def value_key(component_id: str, path: str) -> str:
    return f"{value_key_prefix(component_id)}{path}"


def has_component(obj: bpy.types.Object, component_id: str) -> bool:
    return component_id in enabled_component_ids(obj)


def stored_value(obj: bpy.types.Object, component_id: str, path: str, default=None):
    """One authored value as stored, or ``default`` -- for callers that need a single field
    (the navmesh bake asks one question of the rigidbody) without building a payload."""
    return obj.get(value_key(component_id, path), default)


#: Engine components this host DERIVES from real Blender data, and which the SCHEMA does not
#: already say so about: the mesh pipeline owns renderable, so authoring it as a form would fight
#: the pipeline that already writes it. The Components panel shows it as a read-only row so the
#: panel still lists everything the entity exports.
#:
#: Light and sprite-animation are deliberately NOT here. The schema marks both ``authoredBy`` on
#: the component itself, so :func:`is_authorable` already excludes them — listing them again would
#: be a second copy of a fact the document states. Anything the schema can say, let it say.
HOST_DERIVED_IDS = frozenset({
    component_ids.RENDERABLE,
    # What an object IS CALLED, WHERE IT STANDS, and WHICH MATERIALS override its mesh. All three
    # are read off the Blender object by the export walk, so there is nothing for a form to add —
    # and a panel offering them would let an author type a second, disagreeing answer.
    component_ids.NAME,
    component_ids.TRANSFORM,
    component_ids.MATERIALS,
})

def host_list_field(component: authoring.AuthoredComponentSchema):
    """The schema field that MAKES this component a host list — an array whose items are authored
    by pointing at a host object — or None.

    The same signal the Godot host reads, so a component the engine newly declares this way needs
    no change here beyond a collection to put it in."""
    for field in component.fields:
        items = field.items
        if field.type == authoring.TYPE_ARRAY and items is not None and items.authored_by:
            return field
    return None


def is_host_list(component: authoring.AuthoredComponentSchema) -> bool:
    """Whether this component's body is a host-object list rather than form fields.

    THE SCHEMA DECIDES, and nothing else does. This used to fall back to a hand-written map of two
    engine ids, which is what made host references an engine privilege: a game component saying the
    same thing about itself was detected and then had nowhere to store a pointer."""
    return host_list_field(component) is not None


def is_authorable(component: authoring.AuthoredComponentSchema) -> bool:
    """A component authored entirely by pointing at a host object (``authoredBy`` on the
    component itself — light, sprite-animation) has nothing this host can fill in as a form,
    and :data:`HOST_DERIVED_IDS` are derived outright. Everything else — the game's components,
    the engine's plain-field ones (identity, agent, rigidbody, audio, particles), and the
    host-list ones (collider, interactable) — is authored in the Components panel."""
    return component.authored_by is None and component.id not in HOST_DERIVED_IDS


def host_ref_key(component: authoring.AuthoredComponentSchema) -> str:
    """The key a component's host references are stored under: ``<component-id>/<field-path>``.

    Per FIELD, not per component, so a component declaring two host lists keeps them apart —
    which nothing declares today and which costs nothing to allow."""
    field = host_list_field(component)
    return f"{component.id}/{field.name if field is not None else ''}"


@dataclass(frozen=True)
class HostEntry:
    """One object a component references, and enough to remove it again.

    ``key`` says which component field it fills, so the remove operator reaches the right row of
    the one store every host reference lives in."""

    target: object
    index: int
    key: str = ""


def host_entries(
    obj: bpy.types.Object, component: authoring.AuthoredComponentSchema
) -> list[HostEntry]:
    """Every object this entity references for a host-list component, from whichever store holds
    them.

    ONE store for every component, filtered to this one's field. Indices are ABSOLUTE within it, so
    a filtered view still removes the row the panel pointed at."""
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
    """The objects this entity references as its COLLIDERS.

    A named shortcut for the one component two other subsystems ask about by name -- the exporter,
    which derives a static rigidbody from having any, and the navmesh bake, which walks them as
    geometry. Everything else reaches host references through :func:`host_entries` without naming a
    component at all; these two genuinely mean colliders and nothing else.
    """
    component = component_by_id(schema_for_data_dir(data_dir), component_ids.COLLIDER)
    return host_entries(obj, component) if component is not None else []


def is_present(obj: bpy.types.Object, component: authoring.AuthoredComponentSchema) -> bool:
    """Whether the Components panel should show this component on this entity. Form components
    are present when enabled; a host-list component is ALSO present when its collection has
    entries, because build scripts (and every pre-unification .blend) fill the lists without
    ever touching the marker — and the export is driven by the lists either way."""
    if component.id in enabled_component_ids(obj):
        return True
    return bool(host_entries(obj, component))


def enable_component(obj: bpy.types.Object, component: authoring.AuthoredComponentSchema) -> None:
    enabled = enabled_component_ids(obj)
    if component.id not in enabled:
        obj[ENABLED_KEY] = [*enabled, component.id]

    if is_host_list(component):
        # The body is the pointer collection, not form fields — creating ID properties here
        # would grow a second copy of data the collider objects already are. (Interactable's
        # DisplayName stays derived from the object's name, matching the export.)
        return

    fields, _ = authoring.flatten(component)
    for field in fields:
        key = value_key(component.id, field.path)
        if len(key) > MAX_KEY_LENGTH:
            # Blender's own error for an over-long property name says only that a name was too
            # long -- not which one, and not from where. Name the field: the fix is to shorten it
            # in the game's C#, and nothing else in the message points there.
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

    # Object REFERENCES get a key too, holding the referenced object's NAME. A name rather than
    # an ID pointer because the rest of this store is names and strings the Godot host keys
    # identically, and because a dangling name is a diagnosable export warning while a dangling
    # pointer is a crash. Nothing is mirrored: the reference IS the authoring surface, and the
    # numbers only exist at export.
    for host in authoring.flatten(component)[1]:
        if not host.is_authorable:
            continue
        key = value_key(component.id, host.path)
        # The same guard the field loop above applies, and for the same reason: an over-long
        # property name raises out of Blender saying only that a name was too long. Unguarded,
        # this was the one ID-property write in the addon that could throw instead of warn — out of
        # the Add-component button or the sync click, with nothing naming the field. A reference's
        # path is as capable of being long as a field's (a nested one carries its parent's name
        # too), so it needs the same treatment.
        if len(key) > MAX_KEY_LENGTH:
            log.warn(
                f"'{component.display_name}.{host.path}' does not fit Blender's "
                f"{MAX_KEY_LENGTH}-character property name limit ({len(key)} used). "
                "It is NOT authorable; shorten the field name."
            )
            continue
        if key not in obj:
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

    # A host-list component's data is its object references; removing the component and leaving
    # them would keep exporting it, which makes "remove" a lie. Matched by the key's component half
    # so this needs no schema — disable is reachable for a component the schema no longer declares,
    # which is exactly when its rows most need clearing.
    props = getattr(obj, "paradise", None)
    refs = getattr(props, "host_refs", None) if props is not None else None
    if refs is not None:
        prefix = f"{component_id}/"
        for index in reversed([i for i, item in enumerate(refs) if item.key.startswith(prefix)]):
            refs.remove(index)


def values_for(obj: bpy.types.Object, component: authoring.AuthoredComponentSchema) -> dict:
    """The stored values as the plain ``{path: value}`` mapping the payload builder takes.
    Fields never stored (schema grew since the component was enabled) simply stay absent, and
    the builder fills their defaults."""
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


def bake_leaf_refs(
    obj: bpy.types.Object,
    component: authoring.AuthoredComponentSchema,
    values: dict,
    paths=None,  # ExportPaths
    meshes=None,  # MeshExporter
) -> dict:
    """Fill each reference whose value IS the reference: a mesh, another object, a file.

    The third bake, and the one whose kinds differ in what they RESOLVE rather than in what they
    read off a pose:

    * ``mesh``   -- the referenced object's geometry, written out as a GLB, baked as the
                    data-relative field naming it. Pointing at ITSELF is the normal case, which is
                    why this one does not warn about self-reference the way the pose bake does: an
                    object usually draws its own mesh, and the slot exists so that "this draws" is
                    something an author SAYS rather than something an exporter infers from the
                    object happening to have mesh data.
    * ``entity`` -- the referenced object's NAME, verbatim. Nothing is read off it at all: the name
                    is the reference, reduced to the one thing every exported object carries.
    * ``asset``  -- a path the author picked, normalized to a data-relative field.

    An unassigned or dangling reference leaves the value alone and the payload writes the field's
    own empty value. The runtime is then free to refuse it, and can only refuse it if the export is
    honest rather than inventing a mesh.

    ``meshes`` is the export's OWN :class:`.mesh.MeshExporter`, not one made here: it deduplicates
    by mesh datablock, so a throwaway would re-export the same GLB once per object pointing at it.
    A caller with none — the panel, a test asking what a component would carry — bakes no mesh
    reference, which is the truthful answer for a call that is not an export.
    """
    for host in authoring.flatten(component)[1]:
        if not host.is_authorable or host.leaf_type is None:
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
            # The NAME, and only the name. Whether the target is exported at all is not knowable
            # here — it depends on what IT authors — so a reference to an object that produces no
            # entity is the runtime's refusal to make, by name, where it can say so.
            values[host.path] = target.name
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
    """Fill each ``transform`` reference's leaves from where its referenced object stands.

    The asymmetry the whole mechanism rests on: authored as a REFERENCE, exported as a VALUE. A
    Blender object name means nothing at runtime, so what travels is the pose -- rebased into the
    contract's Y-up basis by the SAME conversion every other transform in the export goes through
    (:func:`..export.transform.decompose_contract`). A second copy of the axis change is how the
    two silently disagree about which way is up.

    Filled BY NAME, and only the names the schema declared: a record takes the part of the pose it
    means. ``Yaw`` is derived the way the runtime derives a heading -- ``atan2`` over the rotated
    +Z -- rather than pulled out of an Euler, because the contract's rotation is a quaternion and
    every other spelling of "which way is it facing" in this pipeline is that atan2.

    An unassigned or dangling reference leaves its leaves ALONE, so the payload builder fills the
    record's own defaults and the runtime sees an unauthored value. Warned, never guessed: a
    reference to an object somebody renamed is an authoring mistake, and a silently-zeroed
    destination is a real place.
    """
    # Imported here rather than at module scope: this module is the store, and export.transform
    # imports the contract package too. Keeping the edge local avoids a cycle between them.
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
    """Fill each ``shape`` reference from the collider drawn on the object it points at.

    The transform bake's sibling, and the same asymmetry: authored as a REFERENCE, exported as a
    VALUE. You point at a collider object, move and size it with Blender's own handles, and what
    travels is the shape -- through :func:`..export.collider.export_shape`, the SAME conversion the
    collider LIST goes through, so a scalar reference and a list entry cannot disagree about what a
    capsule is.

    Only the leaves the record declared are written, so a component takes the part of a shape it
    means. An unassigned or dangling reference leaves them ALONE, and the payload builder then fills
    the record's own defaults -- which the runtime is free to refuse, and can only refuse if the
    export is honest rather than inventing a box.
    """
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
        for leaf in host.bakes:
            if leaf in baked:
                values[f"{host.path}/{leaf}"] = baked[leaf]
    return values


def _yaw_of(rotation) -> float:
    """Rotation about +Y, radians, from a contract ``(x, y, z, w)`` quaternion.

    ``atan2`` over the rotated +Z axis — the same convention ``SceneBinding.HeadingOf`` reads an
    authored matrix with, so an object's facing in Blender and the heading the runtime gives an
    actor are the same number rather than two that happen to look similar.
    """
    x, y, z, w = rotation
    # +Z rotated by the quaternion; only the X and Z components matter for a yaw.
    forward_x = 2.0 * (x * z + w * y)
    forward_z = 1.0 - 2.0 * (x * x + y * y)
    return math.atan2(forward_x, forward_z)


def storage_value(field: authoring.FlatField, value=None):
    """A value coerced to the field's schema type, ready to store as an ID property.

    The stored TYPE is load-bearing: an ID property is typed by first assignment, and Blender
    draws (and keeps) that type from then on.

    ``value`` defaults to the field's own default, which is what enabling a component on an
    entity wants. The Game Config panel passes a value read from the config document instead --
    and passes ``None`` for a member the payload omits or spells as JSON null, which lands back
    on the default because an ID property cannot hold nothing.
    """
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
    """Attach doc/range/subtype to an ID property. Takes any ID datablock, not just an Object:
    the Game Config panel keeps the same kind of store on the Scene."""
    try:
        ui = store.id_properties_ui(key)
    except TypeError:
        return  # strings and some array types carry no UI metadata; nothing to attach

    options: dict[str, object] = {}
    if field.doc:
        options["description"] = field.doc
    if field.type in (authoring.TYPE_FLOAT, authoring.TYPE_INT):
        if field.minimum is not None:
            options["min"] = field.minimum
        if field.maximum is not None:
            options["max"] = field.maximum
    if field.type == authoring.TYPE_COLOR:
        # COLOR_GAMMA, not COLOR: the picker then treats the stored floats as display-space,
        # which is how the Godot inspector's picker treats them -- same swatch, same numbers
        # in the exported document. Plain COLOR would run them through the view transform and
        # the two hosts would disagree about what "that orange" is.
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


# --------------------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------------------


def build_component_payloads(obj: bpy.types.Object, data_dir: str, paths=None, meshes=None) -> list:
    """Every enabled component as ``(id, type, payload)`` triples, in schema order so two exports
    of an unchanged scene are identical. The exporter routes each: engine ids into their typed
    slots (``contract.authoring_router``), game ids into ``Components.Custom``.

    The ``type`` rides along because a game component's payload carries it onto the wire — it is
    what makes an id that fails to resolve diagnosable, and the engine matches it exactly, so it
    is passed through from the schema rather than derived from anything here.

    A component enabled on the object but missing from the current schema is skipped WITH a
    warning -- it means the game removed or renamed the type, and silently dropping the
    authored values is exactly the failure mode the warning exists to name.
    """
    enabled = enabled_component_ids(obj)
    if not enabled:
        return []

    document = schema_for_data_dir(data_dir)
    pairs = []
    for component in document.components:  # already type-ordered by merge()
        if component.id not in enabled:
            continue
        if component.id == component_ids.COLLIDER:
            # The one component still exported by a dedicated path, and not for storage reasons:
            # a collider list also DERIVES a static rigidbody, which is a rule about physics rather
            # than about payloads. See _build_components.
            continue
        if is_host_list(component):
            # Every other host list, the engine's and a game's alike, exports here from the objects
            # it references -- with no Python that knows any component's name.
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
        values = bake_transform_refs(obj, component, values_for(obj, component))
        values = bake_shape_refs(obj, component, values)
        values = bake_leaf_refs(obj, component, values, paths, meshes)
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
    """A host-list component's payload: its plain fields, plus the shapes its references bake to.

    The array is written under the field's own NAME, which is what the runtime deserializes it by
    -- the same wire shape ``Collider`` has produced all along, reached generically.

    Imported here rather than at module scope: this module is the authoring store, and the export
    package imports it, so a top-level import would close the cycle.
    """
    from ..export.collider import build_colliders

    field = host_list_field(component)
    if field is None:
        return None

    values = bake_transform_refs(obj, component, values_for(obj, component))
    payload = authoring.build_payload(component, bake_shape_refs(obj, component, values))
    payload[field.name] = [
        shape.to_json() for shape in build_colliders(obj, host_entries(obj, component))
    ]
    return payload


# --------------------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------------------

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
