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

import bpy
from bpy.types import Operator

from .. import log
from ..contract import authoring
from ..prefs import resolve_blender_data_dir

__all__ = [
    "build_component_payloads",
    "classes",
    "enabled_component_ids",
    "has_component",
    "schema_for_data_dir",
    "schema_load_error",
    "value_key",
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


def value_key(component_id: str, path: str) -> str:
    return f"{VALUE_PREFIX}{component_id}/{path}"


def has_component(obj: bpy.types.Object, component_id: str) -> bool:
    return component_id in enabled_component_ids(obj)


def stored_value(obj: bpy.types.Object, component_id: str, path: str, default=None):
    """One authored value as stored, or ``default`` -- for callers that need a single field
    (the navmesh bake asks one question of the rigidbody) without building a payload."""
    return obj.get(value_key(component_id, path), default)


#: Engine components this host derives from real Blender data rather than a form: the mesh
#: pipeline owns renderable, the collider empties own collider/interactable, and the lamp
#: datablock owns light. Authoring one of these in the panel would fight the pipeline that
#: already writes it.
HOST_OWNED_IDS = frozenset({
    "paradise.renderable",
    "paradise.collider",
    "paradise.interactable",
    "paradise.light",
})


def is_authorable(component: authoring.AuthoredComponentSchema) -> bool:
    """A component authored entirely by pointing at a host object (``authoredBy`` on the
    component itself — light, sprite-animation) has nothing this host can fill in as a form,
    and the HOST-OWNED ids are derived from real Blender data (see ``HOST_OWNED_IDS``).
    Everything else — the game's components AND the engine's plain-field ones (identity,
    agent, rigidbody, audio, particles) — is authored in the Components panel and routed by
    ``contract.authoring_router`` at export."""
    return component.authored_by is None and component.id not in HOST_OWNED_IDS


def enable_component(obj: bpy.types.Object, component: authoring.AuthoredComponentSchema) -> None:
    enabled = enabled_component_ids(obj)
    if component.id not in enabled:
        obj[ENABLED_KEY] = [*enabled, component.id]

    fields, _ = authoring.flatten(component)
    for field in fields:
        key = value_key(component.id, field.path)
        if key in obj:
            continue  # re-enabling keeps previously authored values
        obj[key] = _storage_value(field)
        _apply_ui_metadata(obj, key, field)


def disable_component(obj: bpy.types.Object, component_id: str) -> None:
    enabled = enabled_component_ids(obj)
    if component_id in enabled:
        enabled.remove(component_id)
        if enabled:
            obj[ENABLED_KEY] = enabled
        else:
            del obj[ENABLED_KEY]

    prefix = f"{VALUE_PREFIX}{component_id}/"
    # Snapshot the keys first: deleting while iterating an IDProperty group is undefined.
    for key in [key for key in obj.keys() if key.startswith(prefix)]:  # noqa: SIM118 -- IDPropertyGroup supports `in` only via .keys()
        del obj[key]


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


def _storage_value(field: authoring.FlatField):
    """A field's initial ID-property value. The stored TYPE is load-bearing: an ID property is
    typed by first assignment, and Blender draws (and keeps) that type from then on."""
    default = field.default
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


def _apply_ui_metadata(obj: bpy.types.Object, key: str, field: authoring.FlatField) -> None:
    try:
        ui = obj.id_properties_ui(key)
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


def is_field_visible(obj: bpy.types.Object, component_id: str, field: authoring.FlatField) -> bool:
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
    """Every enabled component as ``(id, payload)`` pairs, in schema (id) order so two exports
    of an unchanged scene are identical. The exporter routes each pair: engine ids into their
    typed slots (``contract.authoring_router``), game ids into ``Components.Custom``.

    A component enabled on the object but missing from the current schema is skipped WITH a
    warning -- it means the game removed or renamed the type, and silently dropping the
    authored values is exactly the failure mode the warning exists to name.
    """
    enabled = enabled_component_ids(obj)
    if not enabled:
        return []

    document = schema_for_data_dir(data_dir)
    pairs = []
    for component in document.components:  # already id-ordered by merge()
        if component.id not in enabled:
            continue
        if not is_authorable(component):
            log.warn(
                f"'{obj.name}' authors '{component.id}', which is not exportable from this "
                "host (an engine-reserved id, or a component authored only by a host-object "
                "bake). It is NOT exported."
            )
            continue
        payload = authoring.build_payload(component, values_for(obj, component))
        pairs.append((component.id, payload))

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
    enabled = set(enabled_component_ids(obj)) if obj is not None else set()
    _enum_items_cache = [
        (component.id, component.display_name, component.id)
        for component in document.components
        if is_authorable(component) and component.id not in enabled
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
    PARADISE_OT_remove_authored_component,
    PARADISE_OT_sync_authored_component,
    PARADISE_OT_set_authored_enum,
]
