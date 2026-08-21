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
    "COUNT_SUFFIX",
    "MAX_KEY_LENGTH",
    "PREFIX_FORMAT",
    "ConfigStoreError",
    "active_document",
    "add_row",
    "classes",
    "component_key_prefix",
    "config_value_key",
    "count_key",
    "counts_for_store",
    "load_into",
    "loaded_stamp",
    "move_row",
    "payloads_from",
    "prefix_for",
    "remove_row",
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
#:
#: VERSIONED, and the version is load-bearing rather than cosmetic. A store written before row
#: counts existed holds none, so every authored list would draw as empty -- and the next Save
#: would then write ``[]`` over the file's real rows. Changing the suffix makes such a store read
#: as NEVER LOADED, so the panel offers Load instead of a lie. The orphaned ``__stamp`` key is
#: harmless and :func:`_forget_values` still sweeps it, because it matches the same prefix.
_STAMP_SUFFIX = "__stamp2"

#: Suffix marking a stored row COUNT. Re-exported from the contract module, which owns the
#: spelling because its path algebra has to recognize it (see :data:`..contract.authoring.
#: COUNT_SUFFIX`); named here too so the storage layer reads as if it owned its own key grammar.
COUNT_SUFFIX = authoring.COUNT_SUFFIX


class ConfigStoreError(RuntimeError):
    """A config this store cannot hold -- reported to the author rather than raised at them."""


def component_key_prefix(prefix: str, component_id: str) -> str:
    """Everything one component's keys share, in one document's namespace. Defined once so a
    SCAN for them cannot fall out of step with the builders below."""
    return f"{prefix}{authored.key_token(component_id)}/"


def config_value_key(prefix: str, component_id: str, path: str) -> str:
    return f"{component_key_prefix(prefix, component_id)}{path}"


def count_key(prefix: str, component_id: str, array_path: str) -> str:
    """Where one list's row count is stored: ``pc1:<token>/Tables#``.

    A count key is always SHORTER than the leaf keys beneath it, which is why nothing checks it
    against :data:`MAX_KEY_LENGTH` separately -- a row whose leaves fit implies a count that fits.
    """
    return f"{component_key_prefix(prefix, component_id)}{array_path}{COUNT_SUFFIX}"


def counts_for_store(store, prefix: str, component_id: str) -> dict[str, int]:
    """Row counts as :func:`..contract.authoring.outline` takes them, read off the count keys.

    A flat SCAN rather than a schema walk, and that is not laziness: the count of
    ``Tables/1/Entries`` cannot be ADDRESSED without already knowing the count of ``Tables``, so
    a walk would have to interleave reading with descending. Reading every count in one pass
    sidesteps the chicken-and-egg entirely.

    Stale entries -- the count key of a row since removed -- are harmless, because ``outline``
    only ever asks about paths it actually reaches.
    """
    head = component_key_prefix(prefix, component_id)
    counts: dict[str, int] = {}
    for key in store.keys():  # noqa: SIM118
        if not key.startswith(head) or not key.endswith(COUNT_SUFFIX):
            continue
        try:
            counts[key[len(head): -len(COUNT_SUFFIX)]] = int(store[key])
        except (TypeError, ValueError):
            continue
    return counts


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
        # How many rows each list holds is DATA, and the file is the only thing that knows.
        counts = authoring.counts_of(component, payload)
        plan = authoring.outline(component, counts)

        # PLANNED first, committed second. An over-long key is a refusal, and a refusal that had
        # already half-written the store would leave the panel showing a document that is partly
        # this file and partly whatever was there before -- a state no button reports and no
        # author could diagnose.
        writes: list[tuple[str, object]] = [
            (count_key(prefix, component_id, array.path), array.count) for array in plan.arrays
        ]
        writes += [
            (
                config_value_key(prefix, component_id, field.path),
                authored.storage_value(field, authoring.value_at(payload, field.path, None)),
            )
            for field in plan.fields
        ]
        for key, _ in writes:
            if len(key) > MAX_KEY_LENGTH:
                raise ConfigStoreError(_too_long(component_id, prefix, key, counts))

        # Rows are DATA, not schema: reloading a document with FEWER rows than the store already
        # holds must not leave the old tail behind, or the next save would resurrect rows the
        # file no longer has. Plain fields were idempotent under the old code; rows are not.
        _forget_component(store, prefix, component_id)
        for key, value in writes:
            store[key] = value
        for field in plan.fields:
            authored.apply_ui_metadata(
                store, config_value_key(prefix, component_id, field.path), field)
    return unknown


def _too_long(component_id: str, prefix: str, key: str, counts) -> str:
    """Why a key did not fit, and what to do about it.

    Names the path, because the fix is to shorten it in the game's C# and Blender's own error for
    this says only that a name was too long. For a path inside a LIST it also says that the limit
    is data-dependent: the same schema loads at nine rows and refuses at ten, which is otherwise
    an utterly baffling failure to hit for the first time on an ordinary edit.
    """
    path = key[len(prefix):]
    detail = (
        f"'{component_id}.{path}' does not fit Blender's {MAX_KEY_LENGTH}-character property "
        f"name limit ({len(key)} used)."
    )
    if any(part.isdigit() for part in path.split("/")):
        detail += (
            " This path is inside a list, so the limit moves with the row count -- adding rows "
            "will hit it again."
        )
    return detail + " Shorten the component id or the field names in the game's C#."


def _forget_component(store, prefix: str, component_id: str) -> None:
    """Drop one component's stored keys, values and counts alike. The FILE is never touched."""
    head = component_key_prefix(prefix, component_id)
    for key in [key for key in store.keys() if key.startswith(head)]:  # noqa: SIM118
        del store[key]


def _plain(stored):
    """An ID property's value as plain Python.

    An ``IDPropertyArray`` assigned straight back would work, but a list is what every other
    writer here stores, and mixing the two makes a key's type an accident of which code path
    last touched it.
    """
    is_array = hasattr(stored, "__len__") and not isinstance(stored, str)
    return list(stored) if is_array else stored


def values_for_store(store, prefix: str, component: authoring.AuthoredComponentSchema) -> dict:
    """The stored values as the flat ``{path: value}`` mapping ``build_payload`` takes.

    Counts are read from the store rather than taken as an argument, so no caller can forget
    them -- one that did would silently produce a payload with every list emptied.
    """
    counts = counts_for_store(store, prefix, component.id)
    values: dict[str, object] = {}
    for field in authoring.flatten(component, counts)[0]:
        key = config_value_key(prefix, component.id, field.path)
        if key not in store:
            continue
        values[field.path] = _plain(store[key])
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
            component,
            values_for_store(store, prefix, component),
            counts_for_store(store, prefix, component.id),
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


# --------------------------------------------------------------------------------------
# Rows -- adding, removing and reordering a list's entries in the store
# --------------------------------------------------------------------------------------
#
# All three are plain functions over a store, returning an error string or None, with thin
# Operator wrappers below. Keeping the logic bpy-free is what lets the renumbering -- the part
# most likely to be subtly wrong -- be tested without Blender.


def _rewrite_rows(store, prefix: str, component, array_path: str, mapping) -> str | None:
    """Apply a row remapping to every stored key under ``array_path``.

    Three phases, and none of them is optional:

    * **plan** -- every surviving key's new name is computed and length-checked BEFORE anything
      is deleted. A move that swaps row 9 with row 10 can LENGTHEN a key, and a refusal issued
      after the delete would have destroyed the rows it declined to move.
    * **delete the whole span, then write it back** -- removing row 1 of 4 renames row 2 onto
      row 1 while row 1 still exists. Deleting first is what stops the rename from clobbering
      the row it is moving onto, without inventing temporary names.
    * **re-attach UI metadata** -- ``id_properties_ui`` belongs to the KEY, so a row that moves
      loses its tooltip, its slider range and its subtype unless they are applied again.
    """
    head = component_key_prefix(prefix, component.id)
    doomed: list[str] = []
    moved: list[tuple[str, object]] = []
    for key in list(store.keys()):
        if not key.startswith(head):
            continue
        path = key[len(head):]
        if authoring.row_index_of(path, array_path) is None:
            continue
        doomed.append(key)
        target = authoring.renumber(path, array_path, mapping)
        if target is None:
            continue  # this row is going away, and everything beneath it with it
        new_key = head + target
        if len(new_key) > MAX_KEY_LENGTH:
            return (
                f"'{target}' would need {len(new_key)} characters and Blender caps a property "
                f"name at {MAX_KEY_LENGTH}. Nothing was moved."
            )
        moved.append((new_key, _plain(store[key])))

    for key in doomed:
        del store[key]
    for key, value in moved:
        store[key] = value
    _reapply_metadata(store, prefix, component)
    return None


def _reapply_metadata(store, prefix: str, component) -> None:
    """Re-attach every leaf's UI metadata, from what the store NOW says exists."""
    counts = counts_for_store(store, prefix, component.id)
    for field in authoring.flatten(component, counts)[0]:
        key = config_value_key(prefix, component.id, field.path)
        if key in store:
            authored.apply_ui_metadata(store, key, field)


def add_row(store, prefix: str, component, array_path: str) -> str | None:
    """Append one row of schema defaults to a list."""
    counts = counts_for_store(store, prefix, component.id)
    index = counts.get(array_path, 0)
    if index >= authoring.MAX_ROWS:
        return f"'{array_path}' is at the {authoring.MAX_ROWS}-row ceiling."

    counts[array_path] = index + 1
    plan = authoring.outline(component, counts)
    fresh = [f for f in plan.fields if authoring.row_index_of(f.path, array_path) == index]

    writes: list[tuple[str, object]] = [
        (config_value_key(prefix, component.id, field.path), authored.storage_value(field))
        for field in fresh
    ]
    # The new row's own nested lists start empty and SAY so, so the panel draws their Add button
    # rather than nothing at all.
    writes += [
        (count_key(prefix, component.id, array.path), 0)
        for array in plan.arrays
        if authoring.row_index_of(array.path, array_path) == index
    ]
    writes.append((count_key(prefix, component.id, array_path), index + 1))

    for key, _ in writes:
        if len(key) > MAX_KEY_LENGTH:
            return _too_long(component.id, prefix, key, counts) + " Nothing was added."
    for key, value in writes:
        store[key] = value
    for field in fresh:
        authored.apply_ui_metadata(
            store, config_value_key(prefix, component.id, field.path), field)
    return None


def remove_row(store, prefix: str, component, array_path: str, index: int) -> str | None:
    """Delete one row, shifting every row above it down."""
    counts = counts_for_store(store, prefix, component.id)
    count = counts.get(array_path, 0)
    if not 0 <= index < count:
        return "That row is no longer there — reload the document."

    # Written BEFORE the rewrite, not after: _rewrite_rows re-applies UI metadata from what the
    # store then says exists, and a stale count would send it looking for a row just deleted.
    # The count key is not under ``array_path + "/"``, so the rewrite never touches it itself.
    store[count_key(prefix, component.id, array_path)] = count - 1
    return _rewrite_rows(
        store, prefix, component, array_path, authoring.removal_mapping(count, index))


def move_row(store, prefix: str, component, array_path: str, index: int, offset: int) -> str | None:
    """Swap a row with its neighbour. A move off either end is a no-op, not an error -- the
    panel greys those buttons, and a race against a reload should not raise at the author."""
    counts = counts_for_store(store, prefix, component.id)
    count = counts.get(array_path, 0)
    target = index + offset
    if not (0 <= index < count and 0 <= target < count):
        return None
    return _rewrite_rows(
        store, prefix, component, array_path, authoring.swap_mapping(count, index, target))


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
        # Counts matter here: a row enum's path (`Tables/0/Kind`) only exists in an outline that
        # was expanded to rows, so a count-less flatten would show "(no values)" for every enum
        # inside a list.
        counts = counts_for_store(context.scene, self.prefix, self.component)
        for field in authoring.flatten(component, counts)[0]:
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


class _RowOperator(Operator):
    """Shared plumbing for the three row buttons: resolve the component, run, report.

    ``path`` is the array's INSTANCE path (``Tables``, or ``Tables/0/Entries`` for a list nested
    inside a row), which is the only address that distinguishes one row's entries from another's.
    """

    bl_options = {"REGISTER", "UNDO"}

    prefix: bpy.props.StringProperty(options={"HIDDEN"})
    component: bpy.props.StringProperty(options={"HIDDEN"})
    path: bpy.props.StringProperty(options={"HIDDEN"})

    def _component(self, context):
        schema = authored.schema_for_data_dir(resolve_blender_data_dir(context.scene))
        return authored.component_by_id(schema, self.component)

    def _run(self, context, action) -> set[str]:
        component = self._component(context)
        if component is None:
            # The game was rebuilt without this group while the panel was open. Refusing beats
            # editing rows against a schema that no longer describes them.
            self.report({"WARNING"}, f"'{self.component}' is not in the current schema.")
            return {"CANCELLED"}
        failure = action(context.scene, self.prefix, component, self.path)
        if failure:
            self.report({"ERROR"}, failure)
            return {"CANCELLED"}
        return {"FINISHED"}


class PARADISE_OT_config_row_add(_RowOperator):
    """Append a row to this list"""

    bl_idname = "paradise.config_row_add"
    bl_label = "Add Row"

    def execute(self, context):
        return self._run(context, add_row)


class PARADISE_OT_config_row_remove(_RowOperator):
    """Remove this row from the list"""

    bl_idname = "paradise.config_row_remove"
    bl_label = "Remove Row"

    index: bpy.props.IntProperty(options={"HIDDEN"})

    def execute(self, context):
        return self._run(
            context, lambda s, p, c, a: remove_row(s, p, c, a, self.index))


class PARADISE_OT_config_row_move(_RowOperator):
    """Move this row up or down in the list"""

    bl_idname = "paradise.config_row_move"
    bl_label = "Move Row"

    index: bpy.props.IntProperty(options={"HIDDEN"})
    # An enum rather than a signed integer, matching Blender's own *_move operators, so the redo
    # panel reads "Up"/"Down" instead of "-1".
    direction: bpy.props.EnumProperty(
        items=[("UP", "Up", ""), ("DOWN", "Down", "")], default="UP", options={"HIDDEN"})

    def execute(self, context):
        offset = -1 if self.direction == "UP" else 1
        return self._run(
            context, lambda s, p, c, a: move_row(s, p, c, a, self.index, offset))


classes = [
    PARADISE_OT_pick_config_document,
    PARADISE_OT_remove_config_document,
    PARADISE_OT_load_config_document,
    PARADISE_OT_save_config_document,
    PARADISE_OT_set_config_enum,
    PARADISE_OT_config_row_add,
    PARADISE_OT_config_row_remove,
    PARADISE_OT_config_row_move,
]
