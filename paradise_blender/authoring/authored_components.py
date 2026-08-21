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
import uuid

import bpy
from bpy.types import Operator

from .. import log
from ..contract import authoring, component_ids
from ..prefs import resolve_blender_data_dir

__all__ = [
    "apply_ui_metadata",
    "build_component_payloads",
    "classes",
    "enabled_component_ids",
    "has_component",
    "host_list_collection",
    "host_list_field",
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

# One cache entry per data directory: (stamp, document, error). Keyed by directory rather than
# held as a single value because two .blend files in different projects can be open in one
# Blender session.
_cache: dict[str, tuple[tuple[int, int], authoring.AuthoringSchemaDocument, str | None]] = {}


def schema_for_data_dir(data_dir: str) -> authoring.AuthoringSchemaDocument:
    """The game's schema, re-read when the file changes. A missing or unreadable file yields
    an EMPTY document rather than an exception -- the panel reports why (see
    :func:`schema_load_error`), and every other caller sees "no components" instead of dying
    mid-draw or mid-export."""
    path = authoring.schema_path(data_dir)
    stamp = authoring.schema_stamp(path)
    cached = _cache.get(data_dir)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    documents = [authoring.read_engine_schema()]
    error: str | None = None
    if stamp == (0, 0):
        error = (
            f"No '{authoring.SCHEMA_FILE_NAME}' in '{data_dir}'. Build the game project to "
            "dump it (the engine writes the file on every build of an [Authored] assembly)."
        )
    else:
        try:
            with open(path, encoding="utf-8") as file:
                documents.append(authoring.read(file.read()))
        except (OSError, authoring.SchemaError) as failure:
            # Named loudly: the symptom of a silently skipped schema is "my component is
            # missing", which gives an author nothing to go on.
            error = f"'{path}' is not a readable authoring schema: {failure}"
            log.warn(error)

    # Engine first, so a game cannot redefine an engine id — the same merge order as the
    # Godot host's LoadSchema.
    document = authoring.merge(documents)
    _cache[data_dir] = (stamp, document, error)
    return document


def schema_load_error(data_dir: str) -> str | None:
    """Why the schema for this directory is empty, or None when it loaded."""
    schema_for_data_dir(data_dir)  # ensure the cache entry reflects the current stamp
    cached = _cache.get(data_dir)
    return cached[2] if cached is not None else None


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
HOST_DERIVED_IDS = frozenset({component_ids.RENDERABLE})

#: Which of ``obj.paradise``'s pointer collections backs a host-list component.
#:
#: Membership here is NOT how a host list is recognised — :func:`host_list_field` reads that off
#: the schema (an array whose ``items.authoredBy`` names a host object, which is how collider
#: declares its shapes). This map answers only the part no schema can: which BLENDER collection
#: holds them, ID properties being unable to carry object references.
#:
#: Interactable has no schema signal at all — no array, no ``authoredBy`` — and is pure Blender
#: policy, which is why detection falls back to this map's keys.
HOST_LIST_COLLECTIONS = {
    component_ids.COLLIDER: "physics_colliders",
    component_ids.INTERACTABLE: "interaction_colliders",
}


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
    """Whether this component's body is a host-object list rather than form fields."""
    return component.id in HOST_LIST_COLLECTIONS or host_list_field(component) is not None


def is_authorable(component: authoring.AuthoredComponentSchema) -> bool:
    """A component authored entirely by pointing at a host object (``authoredBy`` on the
    component itself — light, sprite-animation) has nothing this host can fill in as a form,
    and :data:`HOST_DERIVED_IDS` are derived outright. Everything else — the game's components,
    the engine's plain-field ones (identity, agent, rigidbody, audio, particles), and the
    host-list ones (collider, interactable) — is authored in the Components panel."""
    return component.authored_by is None and component.id not in HOST_DERIVED_IDS


def host_list_collection(obj: bpy.types.Object, component_id: str):
    """The pointer collection backing a host-list component, or None for every other id."""
    attribute = HOST_LIST_COLLECTIONS.get(component_id)
    if attribute is None:
        return None
    props = getattr(obj, "paradise", None)
    return getattr(props, attribute, None) if props is not None else None


def is_present(obj: bpy.types.Object, component: authoring.AuthoredComponentSchema) -> bool:
    """Whether the Components panel should show this component on this entity. Form components
    are present when enabled; a host-list component is ALSO present when its collection has
    entries, because build scripts (and every pre-unification .blend) fill the lists without
    ever touching the marker — and the export is driven by the lists either way."""
    if component.id in enabled_component_ids(obj):
        return True
    collection = host_list_collection(obj, component.id)
    return collection is not None and len(collection) > 0


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

    # A host-list component's data is its collection; removing the component and leaving the
    # references would keep exporting it, which makes "remove" a lie.
    collection = host_list_collection(obj, component_id)
    if collection is not None:
        collection.clear()


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


def build_component_payloads(obj: bpy.types.Object, data_dir: str) -> list:
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
        if is_host_list(component):
            continue  # exported from the entity's collider lists, never from a payload
        if not is_authorable(component):
            log.warn(
                f"'{obj.name}' authors '{component.id}', which is not exportable from this "
                "host (an engine-reserved id, or a component authored only by a host-object "
                "bake). It is NOT exported."
            )
            continue
        payload = authoring.build_payload(component, values_for(obj, component))
        pairs.append((component.id, component.type, payload))

    for component_id in enabled:
        if component_by_id(document, component_id) is None:
            log.warn(
                f"'{obj.name}' authors '{component_id}', which the current authoring schema "
                "does not declare. It is NOT exported. If the game renamed the component, "
                "re-add it; if the schema is stale, rebuild the game project."
            )
    return pairs


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
