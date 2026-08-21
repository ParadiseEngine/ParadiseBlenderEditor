"""The "Paradise" tab in the 3D viewport sidebar (N-panel).

Organized around the workflow rather than the data model: mark entities, export, play, preview.
Entity-ness is invisible in Blender's outliner (it is a flag, not a node type), so the entity
panel doubles as the only place an author can see whether the selected object is exported.
"""

from __future__ import annotations

import os

import bpy
from bpy.types import Panel

from ..authoring import authored_components as authored
from ..authoring import config_store
from ..authoring.collider import is_collider
from ..authoring.entity import entity_objects, is_entity
from ..contract import authoring as contract_authoring
from ..contract import component_ids, config_document
from ..export import navmesh_preview
from ..export.scene import resolve_scene_name
from ..live import session as live_session
from ..play.host import resolve_runtime_command
from ..prefs import get_preferences, resolve_blender_data_dir, resolve_config_document_path

__all__ = ["classes"]

CATEGORY = "Paradise"


class _ParadisePanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY


class PARADISE_PT_scene(_ParadisePanel, Panel):
    bl_label = "Scene"
    bl_idname = "PARADISE_PT_scene"

    def draw(self, context) -> None:
        layout = self.layout
        settings = context.scene.paradise_project

        column = layout.column(align=True)
        column.prop(settings, "data_dir")
        column.prop(settings, "scene_name_override")

        # The resolved values are shown because both are derived (the //-prefix and the
        # .blend basename), and a surprising output location is a common first confusion.
        box = layout.box()
        box.label(text=f"Output: scenes/{resolve_scene_name(context.scene)}.json", icon="FILE")
        if not bpy.data.filepath:
            box.label(text="Unsaved .blend — exporting to a temp directory", icon="ERROR")
        else:
            box.label(text=resolve_blender_data_dir(context.scene), icon="FILE_FOLDER")

        layout.prop(settings, "export_on_save")
        layout.prop(settings, "prune_data")

        column = layout.column(align=True)
        column.prop(settings, "game_project")
        row = column.row()
        row.enabled = bool(settings.game_project.strip())
        row.prop(settings, "watch_game_project")
        if settings.watch_game_project and settings.game_project.strip():
            from ..pipeline import schema_build

            status = column.row()
            status.enabled = False
            status.label(text=schema_build.status_line(), icon="TIME")
            if schema_build.last_failure():
                alert = column.row(align=True)
                alert.alert = True
                alert.operator("paradise.show_build_errors",
                               text="Game build failed — show errors", icon="ERROR")

        column = layout.column(align=True)
        column.label(text="Lighting")
        column.prop(settings, "shadow_map_size")
        column.prop(settings, "shadow_blur")

        row = layout.row(align=True)
        row.scale_y = 1.4
        row.operator("paradise.export_scene", icon="EXPORT").force = False
        # The rebuild-everything variant, deliberately the small button: it costs a full
        # re-encode of every texture, and is only needed when the exporter itself changed.
        row.operator("paradise.export_scene", text="", icon="FILE_REFRESH").force = True

        row = layout.row(align=True)
        row.operator("paradise.convert_textures", icon="TEXTURE")
        row.operator("paradise.open_data_dir", text="", icon="FILE_FOLDER")

        count = len(entity_objects(context.scene))
        row = layout.row()
        row.operator("paradise.select_entities", text=f"{count} Entity/Entities", icon="RESTRICT_SELECT_OFF")
        row.operator("paradise.repair_guids", text="", icon="FILE_REFRESH")


class PARADISE_PT_scene_navmesh(_ParadisePanel, Panel):
    bl_label = "NavMesh"
    bl_idname = "PARADISE_PT_scene_navmesh"
    bl_parent_id = "PARADISE_PT_scene"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context) -> None:
        layout = self.layout
        settings = context.scene.paradise_project

        # Bake + the preview eye live on one row: the bake is what gives the eye something to
        # show, and the pairing makes that dependency legible.
        row = layout.row(align=True)
        row.scale_y = 1.2
        row.operator("paradise.bake_navmesh", icon="GRID")
        row.prop(
            settings,
            "navmesh_preview",
            text="",
            icon="HIDE_OFF" if settings.navmesh_preview else "HIDE_ON",
        )
        if settings.navmesh_preview and navmesh_preview.find_preview_object() is None:
            layout.label(text="No baked preview yet — Bake NavMesh.", icon="INFO")

        column = layout.column(align=True)
        column.label(text="Agent")
        column.prop(settings, "navmesh_agent_radius")
        column.prop(settings, "navmesh_agent_height")
        column.prop(settings, "navmesh_agent_max_climb")
        column.prop(settings, "navmesh_agent_max_slope")

        column = layout.column(align=True)
        column.label(text="Voxelization")
        column.prop(settings, "navmesh_cell_size")
        column.prop(settings, "navmesh_cell_height")

        # Stored in the .blend and applied on every bake, including export-on-save. The Godot
        # host note matters to anyone authoring the same scene from both tools.
        layout.label(text="Defaults mirror the Godot host's bake.", icon="INFO")


class PARADISE_UL_config_documents(bpy.types.UIList):
    """One row per configured document: its label, and the file it edits."""

    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_prop, _index):
        row = layout.row(align=True)
        row.label(text=item.label.strip() or item.file or "(no file)", icon="TEXT")
        if item.label.strip() and item.file:
            sub = row.row()
            sub.enabled = False
            sub.label(text=item.file)


class PARADISE_PT_config(_ParadisePanel, Panel):
    """Authored components that live in FILES rather than on entities.

    A project declares any number of JSON documents here -- a game's tunables, a level's
    settings, whatever else it keeps as authored payloads -- and each is drawn from the same
    ``data/authoring-schema.json`` the Components panel uses. The addon attaches no meaning to
    any of them: it reads the component ids a file declares and draws whatever the schema says
    those are. A tunable added in the game's C# appears here on the next build.

    Load and Save are buttons rather than automatic on purpose: ``draw()`` may not write ID data,
    and an automatic write would let a stale panel overwrite hand edits to a file that is the
    game's source of truth, not ours.
    """

    bl_label = "Config"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context) -> None:
        layout = self.layout
        scene = context.scene
        settings = scene.paradise_project

        row = layout.row()
        row.template_list(
            "PARADISE_UL_config_documents", "", settings, "config_documents",
            settings, "active_config_document", rows=2)
        side = row.column(align=True)
        side.operator("paradise.pick_config_document", text="", icon="ADD").index = -1
        side.operator("paradise.remove_config_document", text="", icon="REMOVE")

        entry = config_store.active_document(scene)
        if entry is None:
            box = layout.box()
            box.label(text="No config documents in this project.", icon="INFO")
            box.label(text="Press + to pick one from the data directory.")
            return

        # The file is CHOSEN, not typed: the picker lists the config documents actually under
        # the data directory, so a row cannot name a file the runtime could not reach.
        column = layout.column(align=True)
        chooser = column.row(align=True)
        chooser.operator("paradise.pick_config_document",
                         text=entry.file or "Choose a document…",
                         icon="FILE").index = settings.active_config_document
        column.prop(entry, "label")

        path = resolve_config_document_path(scene, entry)
        if not path:
            layout.label(text="Choose a document for this row.", icon="INFO")
            return
        if not os.path.exists(path):
            box = layout.box()
            box.label(text="File not found:", icon="ERROR")
            box.label(text=path)
            return

        prefix = config_store.prefix_for(entry)
        loaded = config_store.loaded_stamp(scene, prefix)
        if loaded is None:
            layout.operator("paradise.load_config_document", icon="IMPORT")
            return

        if loaded != config_document.config_stamp(path):
            # Someone edited the file since it was loaded -- a hand edit, a git checkout, a
            # rebuild. Saving now would overwrite whatever that was.
            alert = layout.row(align=True)
            alert.alert = True
            alert.operator("paradise.load_config_document",
                           text="File changed on disk — reload", icon="ERROR")

        try:
            with open(path, encoding="utf-8") as file:
                document = config_document.read(file.read())
        except (OSError, config_document.ConfigError) as failure:
            # Reported in the panel rather than logged: log.* prints on every redraw.
            box = layout.box()
            box.label(text="Document could not be read:", icon="ERROR")
            box.label(text=str(failure)[:120])
            return

        schema = authored.schema_for_data_dir(resolve_blender_data_dir(scene))
        for component_id in config_document.declared_ids(document):
            component = authored.component_by_id(schema, component_id)
            if component is None:
                box = layout.box()
                box.label(text=f"{component_id} — not in the current schema", icon="ERROR")
                box.label(text="Not editable, and left untouched on save. Rebuild the game.")
                continue
            self._draw_group(layout, scene, prefix, component)

        row = layout.row(align=True)
        row.operator("paradise.save_config_document", icon="EXPORT")
        row.operator("paradise.load_config_document", text="Reload", icon="FILE_REFRESH")

    @staticmethod
    def _draw_group(layout, scene, prefix, component) -> None:
        box = layout.box()
        box.label(text=component.display_name, icon="PROPERTIES")

        counts = config_store.counts_for_store(scene, prefix, component.id)
        plan = contract_authoring.outline(component, counts)
        _draw_container(box, scene, prefix, component, _RowIndex.of(plan), container="")


class _RowIndex:
    """Which leaves and which lists hang directly off each ROW.

    Keyed by the nearest enclosing row (``Tables/0/Entries/1``), not by every object level: a
    plain composed field keeps drawing flat under its slash path (``Box/SizeX``) exactly as it
    always has, because changing that would churn every existing panel for a feature about lists.

    Built once per group so drawing a row is a lookup rather than a scan of the whole outline.
    """

    def __init__(self, leaves, arrays) -> None:
        self.leaves = leaves
        self.arrays = arrays

    @classmethod
    def of(cls, plan) -> _RowIndex:
        leaves: dict[str, list] = {}
        arrays: dict[str, list] = {}
        for field in plan.fields:
            leaves.setdefault(contract_authoring.row_container_of(field.path), []).append(field)
        for array in plan.arrays:
            arrays.setdefault(contract_authoring.row_container_of(array.path), []).append(array)
        return cls(leaves, arrays)

    def leaf_at(self, path: str):
        """The single leaf of a scalar row (``Tags/0``), whose container is the row itself."""
        for field in self.leaves.get(contract_authoring.row_container_of(path), ()):
            if field.path == path:
                return field
        return None


def _draw_container(layout, scene, prefix, component, index, container: str) -> None:
    """One row's own leaves, then the lists nested inside it. ``container=""`` is the component."""
    column = layout.column(align=True)
    for field in index.leaves.get(container, ()):
        _draw_leaf(
            column, scene, prefix, component, field,
            label=contract_authoring.relative_to(field.path, container))
    for array in index.arrays.get(container, ()):
        _draw_array(layout, scene, prefix, component, index, array)


def _draw_leaf(layout, scene, prefix, component, field, label: str) -> None:
    if field is None or not authored.is_field_visible(scene, component.id, field):
        return
    key = config_store.config_value_key(prefix, component.id, field.path)
    if key not in scene:
        return  # the schema gained a field since the load; Reload picks it up
    if field.type == contract_authoring.TYPE_ENUM:
        # An ID property cannot drive an enum widget, so the value is picked through an operator.
        row = layout.row(align=True)
        row.label(text=label)
        picker = row.operator(
            "paradise.set_config_enum", text=str(scene.get(key, "")), icon="DOWNARROW_HLT")
        picker.prefix = prefix
        picker.component = component.id
        picker.path = field.path
    else:
        layout.prop(scene, f'["{key}"]', text=label)


def _draw_array(layout, scene, prefix, component, index, array) -> None:
    """A list: a header with its Add button, then one box per row.

    Drawn by hand rather than with ``template_list`` because that needs an RNA
    ``CollectionProperty`` of registered structs. These rows live in ID properties keyed by
    string, on a schema that changes every game build, and a row here is a whole sub-form rather
    than one line. The pattern instead follows ``_draw_host_list_component`` below: a box, a row
    per item, and operators carrying an index.
    """
    box = layout.box()
    header = box.row(align=True)
    header.label(text=f"{array.label}  ({array.count})", icon="LINENUMBERS_ON")
    add = header.operator("paradise.config_row_add", text="", icon="ADD")
    add.prefix, add.component, add.path = prefix, component.id, array.path

    if array.count == 0:
        # Said explicitly: an empty list and a list the panel cannot draw look identical
        # otherwise, and the author has no way to tell which they are looking at.
        note = box.row()
        note.enabled = False
        note.label(text="Empty — press + to add a row", icon="BLANK1")
        return

    for row_index in range(array.count):
        row_path = f"{array.path}/{row_index}"
        row_box = box.box()
        head = row_box.row(align=True)
        head.label(text=f"{row_index}   {_row_title(scene, prefix, component, array, row_path)}")

        buttons = head.row(align=True)
        up = buttons.row(align=True)
        up.enabled = row_index > 0
        move_up = up.operator("paradise.config_row_move", text="", icon="TRIA_UP")
        move_up.prefix, move_up.component, move_up.path = prefix, component.id, array.path
        move_up.index, move_up.direction = row_index, "UP"

        down = buttons.row(align=True)
        down.enabled = row_index < array.count - 1
        move_down = down.operator("paradise.config_row_move", text="", icon="TRIA_DOWN")
        move_down.prefix, move_down.component, move_down.path = prefix, component.id, array.path
        move_down.index, move_down.direction = row_index, "DOWN"

        drop = buttons.operator("paradise.config_row_remove", text="", icon="X")
        drop.prefix, drop.component, drop.path = prefix, component.id, array.path
        drop.index = row_index

        if array.rows_are_records:
            _draw_container(row_box, scene, prefix, component, index, container=row_path)
        else:
            # A scalar row IS one widget; there is no container to walk into.
            _draw_leaf(row_box, scene, prefix, component, index.leaf_at(row_path), label="")


def _row_title(scene, prefix, component, array, row_path: str) -> str:
    """A row's own name, so the header reads "0  Rubble" rather than just "0"."""
    if not array.row_title_path:
        return ""
    key = config_store.config_value_key(
        prefix, component.id, f"{row_path}/{array.row_title_path}")
    return str(scene.get(key, "") or "")


class PARADISE_PT_play(_ParadisePanel, Panel):
    bl_label = "Play & Preview"
    bl_idname = "PARADISE_PT_play"

    def draw(self, context) -> None:
        layout = self.layout

        # The runtime host is machine-scoped (an absolute path), so it is stored in addon
        # preferences -- but Play is dead until it resolves, and "No Paradise runtime found"
        # is not actionable from a panel that offers no way to fix it. So the same property is
        # editable here. warn=False: this resolves on every redraw.
        preferences = get_preferences(context)
        command = resolve_runtime_command(warn=False)

        box = layout.box()
        box.label(text="Runtime", icon="PLAY")
        box.prop(preferences, "runtime_host", text="")
        if command is None:
            box.label(text="No runtime found — set a path above", icon="ERROR")
            box.label(text="An executable, or a host .csproj")
        elif preferences.runtime_host.strip():
            # No path echo -- the field above already shows it.
            box.label(text="Ready", icon="CHECKMARK")
        else:
            box.label(text=f"Auto-detected: {os.path.basename(command[0])}", icon="CHECKMARK")

        row = layout.row()
        row.scale_y = 1.4
        row.enabled = command is not None
        row.operator("paradise.play", icon="PLAY")

        box = layout.box()
        running = live_session.is_running()
        box.label(
            text="Live preview: connected" if running else "Live preview: stopped",
            icon="LINKED" if running else "UNLINKED",
        )
        row = box.row(align=True)
        if running:
            row.operator("paradise.live_stop", icon="PAUSE")
            row.operator("paradise.live_resync", text="", icon="FILE_REFRESH")
        else:
            row.operator("paradise.live_start", icon="LINKED")


class PARADISE_PT_entity(_ParadisePanel, Panel):
    bl_label = "Entity"
    bl_idname = "PARADISE_PT_entity"

    @classmethod
    def poll(cls, context) -> bool:
        return context.active_object is not None

    def draw(self, context) -> None:
        layout = self.layout
        obj = context.active_object

        if not is_entity(obj):
            layout.label(text=f"'{obj.name}' is not exported.", icon="INFO")
            layout.operator("paradise.make_entity", icon="ADD")
            return

        props = obj.paradise

        header = layout.row(align=True)
        header.label(text=obj.name, icon="OBJECT_DATA")
        header.operator("paradise.clear_entity", text="", icon="X")

        # Only the HOST data lives here (see authoring/entity.py); everything that used to be
        # a fixed field -- kind, agent, sprite, particles, audio, body -- is a schema-driven
        # component in the Components section below.
        layout.prop(props, "model_path")

        if props.entity_guid:
            row = layout.row()
            row.enabled = False
            row.label(text=props.entity_guid, icon="KEYINGSET")


class PARADISE_PT_entity_components(_ParadisePanel, Panel):
    """The game's own components, driven by ``<data>/authoring-schema.json``.

    The Blender counterpart of the Godot host's AuthoredEntityNode inspector: the game declares
    a component once (a C# record marked [Authored]), a build dumps the schema, and this panel
    draws it -- no addon change per component. See ``authoring/authored_components.py``.
    """

    bl_label = "Components"
    bl_parent_id = "PARADISE_PT_entity"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context) -> bool:
        return context.active_object is not None and is_entity(context.active_object)

    def draw(self, context) -> None:
        layout = self.layout
        obj = context.active_object
        data_dir = resolve_blender_data_dir(context.scene)
        document = authored.schema_for_data_dir(data_dir)

        from ..pipeline import schema_build

        if schema_build.last_failure():
            # This panel is where a stale dropdown is NOTICED — the author is looking for a
            # component that is not here — so the failure that explains it belongs here too.
            alert = layout.row(align=True)
            alert.alert = True
            alert.operator("paradise.show_build_errors",
                           text="Game build failed — components may be stale", icon="ERROR")

        if authored.schema_load_error(data_dir) is not None:
            box = layout.box()
            box.label(text="No game authoring schema found.", icon="INFO")
            box.label(text="Build the game project to dump it (see console).")
            box.operator("paradise.build_game_schema", icon="FILE_REFRESH")
            # The engine's own components are still available below: the vendored engine
            # schema needs no game build.

        # One list, everything the entity exports: derived components as read-only rows,
        # host-list components (colliders) with their reference lists, form components with
        # their schema fields — in the schema's stable id order.
        for component in document.components:
            if not authored.is_authorable(component):
                _draw_derived_components(layout, obj, component)
            elif authored.is_host_list(component):
                if authored.is_present(obj, component):
                    _draw_host_list_component(layout, context, obj, component)
            elif component.id in authored.enabled_component_ids(obj):
                self._draw_component(layout, obj, component)

        stale = [
            component_id for component_id in authored.enabled_component_ids(obj)
            if authored.component_by_id(document, component_id) is None
        ]
        for component_id in stale:
            box = layout.box()
            row = box.row(align=True)
            row.label(text=f"{component_id} — not in the current schema", icon="ERROR")
            remove = row.operator("paradise.remove_authored_component", text="", icon="X")
            remove.component = component_id
            box.label(text="Not exported. Rebuild the game, or remove it here.")

        layout.operator("paradise.add_authored_component", icon="ADD")

    @staticmethod
    def _draw_component(layout, obj, component) -> None:
        box = layout.box()
        header = box.row(align=True)
        header.label(text=component.display_name, icon="PROPERTIES")
        remove = header.operator("paradise.remove_authored_component", text="", icon="X")
        remove.component = component.id

        fields, hosts = contract_authoring.flatten(component)
        missing = [f for f in fields if authored.value_key(component.id, f.path) not in obj]
        if missing:
            # The schema grew since this component was enabled. Draw() may not write ID data,
            # so the fields are created by an operator click rather than silently here.
            sync = box.operator(
                "paradise.sync_authored_component",
                text=f"Schema gained {len(missing)} field(s) — click to edit",
                icon="FILE_REFRESH",
            )
            sync.component = component.id

        column = box.column(align=True)
        for field in fields:
            if not authored.is_field_visible(obj, component.id, field):
                continue
            key = authored.value_key(component.id, field.path)
            if key not in obj:
                continue  # pending the sync click above
            if field.type == contract_authoring.TYPE_ENUM:
                row = column.row(align=True)
                row.label(text=field.path)
                picker = row.operator(
                    "paradise.set_authored_enum", text=str(obj.get(key, "")), icon="DOWNARROW_HLT"
                )
                picker.component = component.id
                picker.path = field.path
            else:
                column.prop(obj, f'["{key}"]', text=field.path)

        for host in hosts:
            row = box.row()
            row.enabled = False
            row.label(text=f"{host.path} — baked from {host.kind}; not authored in Blender yet",
                      icon="DECORATE_LINKED")

        # Authorable lists are editable in CONFIG DOCUMENTS but not on an entity: an entity's key
        # budget is tighter, and exporting rows from here would change what every scene emits.
        # Said out loud regardless, because this panel's job is to be a complete inventory of what
        # the entity exports -- a member that simply vanished from it would read as "not there".
        for array in contract_authoring.outline(component).arrays:
            row = box.row()
            row.enabled = False
            row.label(text=f"{array.path} — a list; editable in config documents only",
                      icon="DECORATE_LINKED")


class PARADISE_PT_collider(_ParadisePanel, Panel):
    """The selected collider OBJECT's own shape — not to be confused with an entity's Collider
    component, which is the list of these objects and lives in the Components section."""

    bl_label = "Collider Shape"
    bl_idname = "PARADISE_PT_collider"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context) -> bool:
        return context.active_object is not None

    def draw(self, context) -> None:
        layout = self.layout
        obj = context.active_object

        if not is_collider(obj):
            layout.operator("paradise.make_collider", icon="MESH_CUBE")
            return

        props = obj.paradise_collider
        column = layout.column(align=True)
        column.prop(props, "shape")
        column.prop(props, "size_source")

        if props.size_source == "EXPLICIT":
            if props.shape == "Box":
                column.prop(props, "size")
            elif props.shape == "Sphere":
                column.prop(props, "radius")
            else:
                column.prop(props, "radius")
                column.prop(props, "height")

        column = layout.column(align=True)
        column.prop(props, "is_trigger")
        column.prop(props, "is_static")
        column.prop(props, "layer")


class PARADISE_PT_world(_ParadisePanel, Panel):
    bl_label = "Environment"
    bl_idname = "PARADISE_PT_world"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context) -> bool:
        return context.scene.world is not None

    def draw(self, context) -> None:
        layout = self.layout
        props = context.scene.world.paradise

        layout.label(text="Tone mapping comes from Color Management.", icon="INFO")

        column = layout.column(align=True)
        column.prop(props, "ambient_mode")
        column.prop(props, "ambient_energy")
        column.prop(props, "sky_reflections")

        if props.ambient_mode == "Skybox":
            box = layout.box()
            box.label(text="Sky Gradient")
            column = box.column(align=True)
            column.prop(props, "sky_top_color")
            column.prop(props, "sky_horizon_color")
            column.prop(props, "ground_horizon_color")
            column.prop(props, "ground_bottom_color")
            column = box.column(align=True)
            column.prop(props, "sky_curve")
            column.prop(props, "ground_curve")
            column.prop(props, "sky_energy")
            column.prop(props, "ground_energy")
            column.prop(props, "energy_multiplier")
            column = box.column(align=True)
            column.prop(props, "sun_angle_max")
            column.prop(props, "sun_curve")

        box = layout.box()
        box.prop(props, "ssao_enabled")
        column = box.column(align=True)
        column.enabled = props.ssao_enabled
        column.prop(props, "ssao_radius")
        column.prop(props, "ssao_intensity")
        column.prop(props, "ssao_power")

        box = layout.box()
        box.prop(props, "glow_enabled")
        column = box.column(align=True)
        column.enabled = props.glow_enabled
        column.prop(props, "glow_intensity")
        column.prop(props, "glow_threshold")

        box = layout.box()
        box.prop(props, "fog_enabled")
        column = box.column(align=True)
        column.enabled = props.fog_enabled
        column.prop(props, "fog_color")
        column.prop(props, "fog_density")


class PARADISE_PT_material(Panel):
    """Contract-only material parameters, shown in the Material properties tab.

    Lives next to the material it modifies rather than in the N-panel, because that is where
    an author is when they are thinking about a material.
    """

    bl_label = "Paradise"
    bl_idname = "PARADISE_PT_material"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "material"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context) -> bool:
        return context.material is not None

    def draw(self, context) -> None:
        layout = self.layout
        props = context.material.paradise

        layout.label(text="These affect the engine only, not Blender's viewport.", icon="INFO")

        column = layout.column(align=True)
        column.prop(props, "transmission_factor")
        column.prop(props, "material_kind")

        if props.material_kind:
            column = layout.column(align=True)
            column.prop(props, "emissive_strength")
            column.prop(props, "noise_scale")
            column.prop(props, "flow_speed")
            column.prop(props, "color_a")
            column.prop(props, "color_b")


#: Which assign/remove slot each host-list component's operators use.
_HOST_LIST_SLOTS = {
    component_ids.COLLIDER: "PHYSICS",
    component_ids.INTERACTABLE: "INTERACTION",
}


def _draw_derived_components(layout, obj, component) -> None:
    """A read-only row for a component this host derives outright, so the panel is a complete
    inventory of what the entity exports even for the parts nobody authors."""
    if component.id == component_ids.RENDERABLE:
        props = obj.paradise
        if props.model_path.strip():
            source = f"from model '{props.model_path.strip()}'"
        elif obj.type == "MESH" and obj.data is not None:
            source = f"from mesh '{obj.data.name}'"
        else:
            return  # nothing renderable; a row saying so on every marker empty is noise
    elif component.id == component_ids.LIGHT:
        if obj.type != "LIGHT":
            return
        source = f"baked from this {obj.data.type.lower()} lamp"
    else:
        return

    row = layout.row()
    row.enabled = False
    row.label(text=f"{component.display_name} — {source}", icon="DECORATE_LINKED")


def _draw_host_list_component(layout, context, obj, component) -> None:
    """A component whose body is object references: the entity's collider lists — this host's
    half of the schema's ``authoredBy: shape``. Same box, header and remove button as every
    other component; the fields are just pointers you assign instead of values you type."""
    slot = _HOST_LIST_SLOTS.get(component.id)
    collection = authored.host_list_collection(obj, component.id)
    if slot is None:
        row = layout.row()
        row.enabled = False
        row.label(text=f"{component.display_name} — not authorable in Blender yet", icon="ERROR")
        return

    box = layout.box()
    header = box.row(align=True)
    header.label(text=component.display_name, icon="PROPERTIES")
    assign = header.operator("paradise.assign_colliders", text="", icon="ADD")
    assign.slot = slot
    remove = header.operator("paradise.remove_authored_component", text="", icon="X")
    remove.component = component.id

    if component.id == component_ids.INTERACTABLE:
        note = box.row()
        note.enabled = False
        note.label(text="DisplayName — the object's name", icon="DECORATE_LINKED")

    if collection is None or not len(collection):
        box.label(text="Select collider objects and press +", icon="BLANK1")
        return
    for index, item in enumerate(collection):
        row = box.row(align=True)
        row.label(text=item.target.name if item.target else "<missing>", icon="MESH_CUBE")
        drop = row.operator("paradise.remove_collider", text="", icon="X")
        drop.slot = slot
        drop.index = index


classes = (
    PARADISE_PT_scene,
    PARADISE_PT_scene_navmesh,
    PARADISE_UL_config_documents,
    PARADISE_PT_config,
    PARADISE_PT_play,
    PARADISE_PT_entity,
    PARADISE_PT_entity_components,
    PARADISE_PT_collider,
    PARADISE_PT_world,
    PARADISE_PT_material,
)
