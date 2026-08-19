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
from ..authoring.collider import is_collider
from ..authoring.entity import entity_objects, is_entity
from ..contract import authoring as contract_authoring
from ..export import navmesh_preview
from ..export.scene import resolve_scene_name
from ..live import session as live_session
from ..play.host import resolve_runtime_command
from ..prefs import get_preferences, resolve_blender_data_dir

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


class PARADISE_PT_entity_colliders(_ParadisePanel, Panel):
    """Host-object references the exporter bakes into the collider and interactable
    components -- this host's half of the schema's ``authoredBy: shape``. Body PROPERTIES
    (type, mass, friction) are the paradise.rigidbody component in the Components section;
    a derived static body is emitted automatically whenever physics colliders exist."""

    bl_label = "Colliders"
    bl_parent_id = "PARADISE_PT_entity"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context) -> bool:
        return context.active_object is not None and is_entity(context.active_object)

    def draw(self, context) -> None:
        layout = self.layout
        props = context.active_object.paradise

        _draw_collider_list(layout, context, props.physics_colliders, "PHYSICS", "Physics Colliders")
        _draw_collider_list(
            layout, context, props.interaction_colliders, "INTERACTION", "Interaction Colliders"
        )


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

        if authored.schema_load_error(data_dir) is not None:
            box = layout.box()
            box.label(text="No game authoring schema found.", icon="INFO")
            box.label(text="Build the game project to dump it (see console).")
            box.operator("paradise.build_game_schema", icon="FILE_REFRESH")
            # The engine's own components are still available below: the vendored engine
            # schema needs no game build.

        stale: list[str] = []
        for component_id in authored.enabled_component_ids(obj):
            component = authored.component_by_id(document, component_id)
            if component is None:
                stale.append(component_id)
                continue
            self._draw_component(layout, obj, component)

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


class PARADISE_PT_collider(_ParadisePanel, Panel):
    bl_label = "Collider"
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


def _draw_collider_list(layout, context, collection, slot: str, label: str) -> None:
    box = layout.box()
    row = box.row()
    row.label(text=label)
    operator = row.operator("paradise.assign_colliders", text="", icon="ADD")
    operator.slot = slot

    if not len(collection):
        box.label(text="None", icon="BLANK1")
        return

    for index, item in enumerate(collection):
        row = box.row(align=True)
        row.label(text=item.target.name if item.target else "<missing>", icon="MESH_CUBE")
        remove = row.operator("paradise.remove_collider", text="", icon="X")
        remove.slot = slot
        remove.index = index


classes = (
    PARADISE_PT_scene,
    PARADISE_PT_scene_navmesh,
    PARADISE_PT_play,
    PARADISE_PT_entity,
    PARADISE_PT_entity_components,
    PARADISE_PT_entity_colliders,
    PARADISE_PT_collider,
    PARADISE_PT_world,
    PARADISE_PT_material,
)
