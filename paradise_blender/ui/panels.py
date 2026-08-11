"""The "Paradise" tab in the 3D viewport sidebar (N-panel).

Organized around the workflow rather than the data model: mark entities, export, play, preview.
Entity-ness is invisible in Blender's outliner (it is a flag, not a node type), so the entity
panel doubles as the only place an author can see whether the selected object is exported.
"""

from __future__ import annotations

import os

import bpy
from bpy.types import Panel

from ..authoring.collider import is_collider
from ..authoring.entity import entity_objects, is_entity
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

        row = layout.row(align=True)
        row.scale_y = 1.4
        row.operator("paradise.export_scene", icon="EXPORT")

        row = layout.row(align=True)
        row.operator("paradise.convert_textures", icon="TEXTURE")
        row.operator("paradise.open_data_dir", text="", icon="FILE_FOLDER")

        count = len(entity_objects(context.scene))
        row = layout.row()
        row.operator("paradise.select_entities", text=f"{count} Entity/Entities", icon="RESTRICT_SELECT_OFF")
        row.operator("paradise.repair_guids", text="", icon="FILE_REFRESH")


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

        column = layout.column(align=True)
        column.prop(props, "kind")
        if props.kind == "CUSTOM":
            column.prop(props, "custom_kind", text="")
        column.prop(props, "active_on_load")
        column.prop(props, "model_path")
        column.prop(props, "initial_animation")

        if props.entity_guid:
            row = layout.row()
            row.enabled = False
            row.label(text=props.entity_guid, icon="KEYINGSET")


class PARADISE_PT_entity_physics(_ParadisePanel, Panel):
    bl_label = "Physics"
    bl_parent_id = "PARADISE_PT_entity"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context) -> bool:
        return context.active_object is not None and is_entity(context.active_object)

    def draw(self, context) -> None:
        layout = self.layout
        props = context.active_object.paradise

        layout.prop(props, "is_dynamic_body")
        column = layout.column(align=True)
        column.enabled = props.is_dynamic_body
        column.prop(props, "body_mass")
        column.prop(props, "body_linear_damping")

        column = layout.column(align=True)
        # Restitution and friction matter on static bodies too: they define the bounce and grip
        # dynamic bodies get off this surface. So they stay enabled regardless.
        column.prop(props, "body_restitution")
        column.prop(props, "body_friction")

        _draw_collider_list(layout, context, props.physics_colliders, "PHYSICS", "Physics Colliders")
        _draw_collider_list(
            layout, context, props.interaction_colliders, "INTERACTION", "Interaction Colliders"
        )


class PARADISE_PT_entity_agent(_ParadisePanel, Panel):
    bl_label = "Agent"
    bl_parent_id = "PARADISE_PT_entity"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context) -> bool:
        return context.active_object is not None and is_entity(context.active_object)

    def draw(self, context) -> None:
        layout = self.layout
        props = context.active_object.paradise

        layout.prop(props, "is_agent")
        column = layout.column(align=True)
        column.enabled = props.is_agent
        column.prop(props, "move_speed")
        column.prop(props, "acceleration")
        column.prop(props, "idle_animation")
        column.prop(props, "walk_animation")
        if props.is_agent:
            column.label(text="Agents are excluded from the navmesh bake.", icon="INFO")


class PARADISE_PT_entity_sprite(_ParadisePanel, Panel):
    bl_label = "Sprite & Particles"
    bl_parent_id = "PARADISE_PT_entity"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context) -> bool:
        return context.active_object is not None and is_entity(context.active_object)

    def draw(self, context) -> None:
        layout = self.layout
        props = context.active_object.paradise

        box = layout.box()
        box.prop(props, "sprite_enabled")
        column = box.column(align=True)
        column.enabled = props.sprite_enabled
        column.prop(props, "sprite_sheet")
        row = column.row(align=True)
        row.prop(props, "sprite_columns")
        row.prop(props, "sprite_rows")
        column.prop(props, "sprite_frame_count")
        column.prop(props, "sprite_fps")
        column.prop(props, "sprite_loop")
        column.prop(props, "sprite_billboard")

        box = layout.box()
        box.prop(props, "particle_kind")
        if props.particle_kind == "NONE":
            return

        column = box.column(align=True)
        column.prop(props, "particle_max_count")
        column.prop(props, "particle_emit_rate")
        column.prop(props, "particle_lifetime")
        column.prop(props, "particle_speed")
        column.prop(props, "particle_spread_degrees")
        column.prop(props, "particle_gravity")
        column.prop(props, "particle_drag")
        row = column.row(align=True)
        row.prop(props, "particle_start_size")
        row.prop(props, "particle_end_size")
        column.prop(props, "particle_color")
        column.prop(props, "particle_seed")

        if props.particle_kind == "Sprite":
            column = box.column(align=True)
            column.prop(props, "particle_sheet")
            row = column.row(align=True)
            row.prop(props, "particle_sheet_columns")
            row.prop(props, "particle_sheet_rows")
            column.prop(props, "particle_sheet_frame_count")
            column.prop(props, "particle_sheet_fps")


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
    PARADISE_PT_play,
    PARADISE_PT_entity,
    PARADISE_PT_entity_physics,
    PARADISE_PT_entity_agent,
    PARADISE_PT_entity_sprite,
    PARADISE_PT_collider,
    PARADISE_PT_world,
    PARADISE_PT_material,
)
