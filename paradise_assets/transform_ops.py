"""Explicit authoring actions for host transform slots."""

from __future__ import annotations

from bpy.props import EnumProperty, StringProperty
from bpy.types import Operator

from .materialize import store, transform_helpers

__all__ = ["classes", "draw"]


class PARADISE_ASSETS_OT_transform_slot(Operator):
    """Choose a transform handle or copy an object's world placement into it"""

    bl_idname = "paradise_assets.transform_slot"
    bl_label = "Transform Placement"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    owner_guid: StringProperty(name="Owner")
    component_id: StringProperty(name="Component")
    field_name: StringProperty(name="Field")
    action: EnumProperty(items=[
        ("CREATE", "Create Handle", "Create a placement handle at this object's position"),
        ("SELECT", "Select Handle", "Select the handle to move or rotate the placement"),
        ("CLEAR", "Clear Placement", "Clear this placement; save to write the change"),
        ("PICK", "Copy Object Placement", "Copy another object's world placement into this field"),
    ])
    target_name: StringProperty(name="Object")

    def invoke(self, context, event):
        if self.action == "PICK":
            return context.window_manager.invoke_props_dialog(self)
        return self.execute(context)

    def draw(self, context):
        self.layout.prop_search(self, "target_name", context.scene, "objects", text="Object")
        self.layout.label(text="Copies its world placement into a movable handle.")

    def execute(self, context):
        from .component_ops import document_object, schema_for

        owner = (store.object_with_guid(context.scene, self.owner_guid)
                 if self.owner_guid else document_object(context))
        if owner is None:
            self.report({"ERROR"}, "Select a document object first")
            return {"CANCELLED"}
        schema = schema_for(context, owner, self.component_id)
        field = schema.resolve(self.field_name) if schema is not None else None
        if field is None or field.authored_by != "transform":
            self.report({"ERROR"}, "This field is not a transform placement")
            return {"CANCELLED"}
        if self.action != "SELECT" and not transform_helpers.editable(owner):
            self.report({"ERROR"}, "Open the prefab that authors this component to edit it")
            return {"CANCELLED"}
        context.view_layer.update()
        empty = transform_helpers.helper_for(owner, self.component_id, self.field_name)
        if self.action == "CLEAR":
            transform_helpers.clear(owner, self.component_id, self.field_name)
            _select(context, owner)
            return {"FINISHED"}
        if self.action == "SELECT":
            if empty is None:
                self.report({"ERROR"}, "This placement has no handle")
                return {"CANCELLED"}
        else:
            target = owner
            if self.action == "PICK":
                target = context.scene.objects.get(self.target_name)
                if target is None:
                    self.report({"ERROR"}, "Choose an object whose placement should be copied")
                    return {"CANCELLED"}
            empty = transform_helpers.assign(
                owner, self.component_id, self.field_name, field, target.matrix_world)
        _select(context, empty)
        return {"FINISHED"}


def _select(context, obj):
    for other in context.selected_objects:
        other.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj


def draw(box, context, obj, component_id: str, item) -> None:
    """One schema transform row; drawing never creates or updates a handle."""
    empty = transform_helpers.helper_for(obj, component_id, item.path)
    row = box.row(align=True)
    row.label(text=f"{item.field.name}: {empty.name if empty is not None else 'Not assigned'}",
              icon="EMPTY_ARROWS")
    buttons = box.row(align=True)
    editable = transform_helpers.editable(obj)
    actions = [("SELECT", "Select Handle")] if empty is not None else [("CREATE", "Create Handle")]
    actions.append(("PICK", "Pick Object"))
    if empty is not None:
        actions.append(("CLEAR", "Clear"))
    for action, label in actions:
        control = buttons.row(align=True)
        control.enabled = action == "SELECT" or editable
        op = control.operator(PARADISE_ASSETS_OT_transform_slot.bl_idname, text=label)
        op.owner_guid = store.guid_of(obj) or ""
        op.component_id = component_id
        op.field_name = item.path
        op.action = action
    if not editable:
        box.label(text="Open the prefab that authors this component to edit it", icon="INFO")


classes = (PARADISE_ASSETS_OT_transform_slot,)
