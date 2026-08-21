"""File-backed authored components: the store behind the Game Config panel.

Authored components live at three scopes in a Paradise project, and this module is the machinery
for the two that are files rather than entities:

* **documents** -- any number of JSON files a project declares, each holding authored payloads.
  A game's tunables and a level's settings are two rows in one list, not two features; the addon
  attaches no meaning to any of them.
* **objects** -- the entity's own components, which are :mod:`.authored_components`' job and are
  not touched here.

The storage is Blender ID properties on the SCENE, which is why so little code is needed: the
helpers in :mod:`.authored_components` only ever use ``get`` / ``[key]`` / ``id_properties_ui``,
and a Scene supports all three exactly as an Object does. The schema half
(:mod:`..contract.authoring`) is reused verbatim -- a config group is an authored component, so
its fields flatten to slash paths and re-nest into a payload by the same code an entity's do.

Two rules the panel over this store lives by, both Blender's rather than ours:

* **Loading is an operator, never a draw.** ``draw()`` may not write ID data, so the values cannot
  be faulted in when the panel first appears.
* **Saving is explicit -- never on export, never on .blend save.** The config file is the game's
  source of truth and is edited by hand; an automatic write from a stale or never-loaded panel
  would replace real tuning with schema defaults, and would look like a successful save.
"""

from __future__ import annotations

import bpy
from bpy.types import Operator

from ..contract import authoring, config_document, writer
from ..prefs import resolve_blender_data_dir, resolve_config_document_path
from . import authored_components as authored

__all__ = [
    "MAX_KEY_LENGTH",
    "PREFIX_FORMAT",
    "ConfigStoreError",
    "active_document",
    "classes",
    "config_value_key",
    "load_into",
    "loaded_stamp",
    "payloads_from",
    "prefix_for",
    "values_for_store",
]

#: Prefix for one document's stored values: ``pc<key>:``, where the key is the document row's own
#: stable id. Distinct from :data:`.authored_components.VALUE_PREFIX` so an entity's components and
#: a document's can never alias, and per-document so two documents declaring the SAME component id
#: stay separate.
#:
#: Terse on purpose. Blender caps an ID property NAME at 63 characters and a key is
#: ``prefix + <compacted id> + "/" + field path``. The id is a GUID and would be 36 characters
#: spent verbatim, so it goes through :func:`.authored_components.key_token` first and costs 22 --
#: without that, ShiningPie's ``FollowYawSmoothingSeconds`` alone would not fit. Every character
#: spent on the prefix is still one a project cannot spend on a readable field name.
PREFIX_FORMAT = "pc{key}:"


def prefix_for(entry) -> str:
    """The ID-property namespace one config document's values live in."""
    return PREFIX_FORMAT.format(key=entry.key)


def active_document(scene):
    """The config document row the panel is editing, or None when the list is empty."""
    documents = scene.paradise_project.config_documents
    index = scene.paradise_project.active_config_document
    return documents[index] if 0 <= index < len(documents) else None

#: Blender's limit on an ID property name. Exceeding it raises a KeyError naming no field, so
#: :func:`load_into` checks first and says which one.
MAX_KEY_LENGTH = 63

#: Where the stamp of the file a store was loaded from is kept, so the panel can notice the file
#: moving underneath it. Suffixed per prefix: two scopes, two files, two stamps.
_STAMP_SUFFIX = "__stamp"


class ConfigStoreError(RuntimeError):
    """A config this store cannot hold -- reported to the author rather than raised at them."""


def config_value_key(prefix: str, component_id: str, path: str) -> str:
    return f"{prefix}{authored.key_token(component_id)}/{path}"


def loaded_stamp(store, prefix: str) -> tuple[int, int] | None:
    """The stamp of the file this store was loaded from, or None when it never was."""
    stored = store.get(prefix + _STAMP_SUFFIX)
    if not isinstance(stored, str):
        return None
    try:
        mtime, _, size = stored.partition(":")
        return (int(mtime), int(size))
    except ValueError:
        return None


def _store_stamp(store, prefix: str, path: str) -> None:
    """Remember which revision of the file the store holds.

    Kept as TEXT, not a pair of ints: a nanosecond mtime is ~1.7e18, and Blender stores an ID
    property integer as a C int, which overflows well below that.
    """
    mtime, size = config_document.config_stamp(path)
    store[prefix + _STAMP_SUFFIX] = f"{mtime}:{size}"


def load_into(store, prefix: str, document, schema: authoring.AuthoringSchemaDocument) -> list[str]:
    """Populate the store from a config document; returns the ids the schema does not declare.

    A field the payload omits gets its schema default rather than being skipped, so the panel
    shows the value the game will actually run on: the game-side reader fills an absent member
    from the record's own initializer, and an editor that left the row blank would be lying about
    what is in effect.
    """
    unknown: list[str] = []
    for component_id in config_document.declared_ids(document):
        component = authored.component_by_id(schema, component_id)
        if component is None:
            unknown.append(component_id)
            continue
        payload = config_document.payload_of(document, component_id) or {}
        for field in authoring.flatten(component)[0]:
            key = config_value_key(prefix, component_id, field.path)
            if len(key) > MAX_KEY_LENGTH:
                # Naming the field matters: the fix is to shorten it in the game's C#, and
                # Blender's own error for this says only that a name was too long.
                raise ConfigStoreError(
                    f"'{component_id}.{field.path}' does not fit Blender's "
                    f"{MAX_KEY_LENGTH}-character property name limit "
                    f"({len(key)} used). Shorten the component id or the field name."
                )
            store[key] = authored.storage_value(field, _at_path(payload, field.path, None))
            authored.apply_ui_metadata(store, key, field)
    return unknown


def _at_path(payload, path: str, fallback):
    """Read a slash path out of a nested payload -- the inverse of the writer's re-nesting.

    Returns ``fallback`` for anything absent, so a member the file omits falls through to the
    field's schema default rather than storing a hole.
    """
    value = payload
    for part in path.split("/"):
        if not isinstance(value, dict) or part not in value:
            return fallback
        value = value[part]
    return fallback if value is None else value


def values_for_store(store, prefix: str, component: authoring.AuthoredComponentSchema) -> dict:
    """The stored values as the flat ``{path: value}`` mapping ``build_payload`` takes."""
    values: dict[str, object] = {}
    for field in authoring.flatten(component)[0]:
        key = config_value_key(prefix, component.id, field.path)
        if key not in store:
            continue
        stored = store[key]
        # IDProperty arrays are not lists; the contract module expects plain Python.
        is_array = hasattr(stored, "__len__") and not isinstance(stored, str)
        values[field.path] = list(stored) if is_array else stored
    return values


def payloads_from(store, prefix: str, document, schema) -> dict[str, dict]:
    """One payload per component the document declares AND the schema still knows.

    An id the schema dropped is deliberately absent from the result, which is what makes the save
    non-destructive for it: :func:`..contract.config_document.merge_payloads` leaves an entry it is
    given no update for exactly as it found it, so a stale group survives a save instead of being
    rewritten from a schema that no longer describes it.
    """
    payloads: dict[str, dict] = {}
    for component_id in config_document.declared_ids(document):
        component = authored.component_by_id(schema, component_id)
        if component is None:
            continue
        payloads[component_id] = authoring.build_payload(
            component, values_for_store(store, prefix, component)
        )
    return payloads


def _read_document(path: str):
    with open(path, encoding="utf-8") as file:
        return config_document.read(file.read())


_document_items_cache: list[tuple[str, str, str]] = []


def _discoverable_items(self, context):
    """Config documents under the data directory, minus the ones already in the list.

    Recomputed on invoke rather than cached across calls: a game build or a git checkout can add
    one at any time, and a dropdown that needed a Blender restart to notice would be worse than
    a directory walk nobody sees. Held in a module global because Blender reads these strings
    lazily and frees anything Python has stopped referencing.
    """
    global _document_items_cache
    scene = context.scene
    taken = {row.file for i, row in enumerate(scene.paradise_project.config_documents)
             if i != self.index}
    found = [f for f in config_document.discover(resolve_blender_data_dir(scene))
             if f not in taken]
    _document_items_cache = [(f, f, "") for f in found] or [("NONE", "(none found)", "")]
    return _document_items_cache


class PARADISE_OT_pick_config_document(Operator):
    """Choose a config document from the data directory"""

    bl_idname = "paradise.pick_config_document"
    bl_label = "Choose Config Document"
    bl_options = {"REGISTER", "UNDO"}
    bl_property = "file"

    #: Row to point at the chosen file; -1 adds a new row.
    index: bpy.props.IntProperty(default=-1, options={"HIDDEN"})
    file: bpy.props.EnumProperty(items=_discoverable_items, name="File")

    def invoke(self, context, _event):
        context.window_manager.invoke_search_popup(self)
        return {"FINISHED"}

    def execute(self, context):
        if self.file == "NONE":
            return {"CANCELLED"}
        settings = context.scene.paradise_project

        if self.index < 0:
            entry = settings.config_documents.add()
            # Highest key ever used plus one, not the row count: reusing a key freed by a removal
            # would hand the new row the old one's stored values.
            entry.key = max((row.key for row in settings.config_documents), default=0) + 1
            settings.active_config_document = len(settings.config_documents) - 1
        else:
            entry = settings.config_documents[self.index]
            # Pointing a row at a different file makes its loaded values that file's, not this
            # one's. Drop them so the row reads as unloaded rather than showing the old document.
            _forget_values(context.scene, prefix_for(entry))

        entry.file = self.file
        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


def _forget_values(scene, prefix: str) -> None:
    """Drop everything one document row has stored. The FILE is never touched."""
    for key in [key for key in scene.keys() if key.startswith(prefix)]:  # noqa: SIM118
        del scene[key]


class PARADISE_OT_remove_config_document(Operator):
    """Remove the selected document from the list, and forget its edited values"""

    bl_idname = "paradise.remove_config_document"
    bl_label = "Remove Config Document"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context) -> bool:
        return active_document(context.scene) is not None

    def execute(self, context):
        scene = context.scene
        settings = scene.paradise_project
        entry = active_document(scene)
        if entry is None:
            return {"CANCELLED"}

        # Drop the stored values too. Leaving them would resurrect stale edits under a future
        # row, and they are only a cache of the file -- the file itself is untouched.
        _forget_values(scene, prefix_for(entry))

        settings.config_documents.remove(settings.active_config_document)
        settings.active_config_document = min(
            settings.active_config_document, max(len(settings.config_documents) - 1, 0)
        )
        return {"FINISHED"}


class PARADISE_OT_load_config_document(Operator):
    """Read the selected config document into this scene for editing"""

    bl_idname = "paradise.load_config_document"
    bl_label = "Load"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context) -> bool:
        entry = active_document(context.scene)
        return entry is not None and bool(resolve_config_document_path(context.scene, entry))

    def execute(self, context):
        scene = context.scene
        entry = active_document(scene)
        if entry is None:
            return {"CANCELLED"}
        prefix = prefix_for(entry)
        path = resolve_config_document_path(scene, entry)
        try:
            document = _read_document(path)
        except (OSError, config_document.ConfigError) as failure:
            self.report({"ERROR"}, f"Could not read '{path}': {failure}")
            return {"CANCELLED"}

        schema = authored.schema_for_data_dir(resolve_blender_data_dir(scene))
        try:
            unknown = load_into(scene, prefix, document, schema)
        except ConfigStoreError as failure:
            self.report({"ERROR"}, str(failure))
            return {"CANCELLED"}
        _store_stamp(scene, prefix, path)

        if unknown:
            # Named rather than counted: the fix differs per id (rebuild the game, or delete a
            # group the game removed), and the author needs to know which one it is.
            self.report(
                {"WARNING"},
                "Loaded, but the authoring schema does not declare: "
                + ", ".join(unknown)
                + ". Rebuild the game project, or remove them from the document.",
            )
        else:
            self.report({"INFO"}, f"Loaded {len(config_document.declared_ids(document))} group(s).")
        return {"FINISHED"}


class PARADISE_OT_save_config_document(Operator):
    """Write the edited values back into the selected config document"""

    bl_idname = "paradise.save_config_document"
    bl_label = "Save"

    @classmethod
    def poll(cls, context) -> bool:
        entry = active_document(context.scene)
        if entry is None or not resolve_config_document_path(context.scene, entry):
            return False
        return loaded_stamp(context.scene, prefix_for(entry)) is not None

    def execute(self, context):
        scene = context.scene
        entry = active_document(scene)
        if entry is None:
            return {"CANCELLED"}
        prefix = prefix_for(entry)
        path = resolve_config_document_path(scene, entry)
        try:
            # Re-read rather than saving against the copy loaded earlier: everything this editor
            # does not understand -- prose keys, the game's own content sections -- has to come
            # from the file as it stands NOW, or a hand edit made since the load is destroyed.
            document = _read_document(path)
        except (OSError, config_document.ConfigError) as failure:
            self.report({"ERROR"}, f"Could not re-read '{path}': {failure}")
            return {"CANCELLED"}

        schema = authored.schema_for_data_dir(resolve_blender_data_dir(scene))
        payloads = payloads_from(scene, prefix, document, schema)
        if not payloads:
            self.report({"WARNING"}, "Nothing to save — load the document first.")
            return {"CANCELLED"}

        try:
            writer.write_json_document(path, config_document.merge_payloads(document, payloads))
        except OSError as failure:
            self.report({"ERROR"}, f"Could not write '{path}': {failure}")
            return {"CANCELLED"}

        _store_stamp(scene, prefix, path)
        self.report({"INFO"}, f"Saved {len(payloads)} group(s) to {path}.")
        return {"FINISHED"}


def _config_enum_items(self, context):
    global _enum_items_cache
    schema = authored.schema_for_data_dir(resolve_blender_data_dir(context.scene))
    component = authored.component_by_id(schema, self.component)
    values: list[str] = []
    if component is not None:
        for field in authoring.flatten(component)[0]:
            if field.path == self.path and field.values:
                values = field.values
    # Held in a module global for the same reason the entity picker's is: Blender reads the
    # callback's strings lazily and frees anything Python is no longer holding.
    _enum_items_cache = [(v, v, "") for v in values] or [("NONE", "(no values)", "")]
    return _enum_items_cache


_enum_items_cache: list[tuple[str, str, str]] = []


class PARADISE_OT_set_config_enum(Operator):
    """Choose a value for an enum field of a config component"""

    bl_idname = "paradise.set_config_enum"
    bl_label = "Set Value"
    bl_options = {"REGISTER", "UNDO"}
    bl_property = "value"

    prefix: bpy.props.StringProperty(options={"HIDDEN"})
    component: bpy.props.StringProperty(options={"HIDDEN"})
    path: bpy.props.StringProperty(options={"HIDDEN"})
    value: bpy.props.EnumProperty(items=_config_enum_items)

    def invoke(self, context, _event):
        context.window_manager.invoke_search_popup(self)
        return {"FINISHED"}

    def execute(self, context):
        if self.value == "NONE":
            return {"CANCELLED"}
        context.scene[config_value_key(self.prefix, self.component, self.path)] = self.value
        # Search popups do not redraw the invoking panel on their own.
        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


classes = [
    PARADISE_OT_pick_config_document,
    PARADISE_OT_remove_config_document,
    PARADISE_OT_load_config_document,
    PARADISE_OT_save_config_document,
    PARADISE_OT_set_config_enum,
]
