"""Blender collection restructuring and CLI orchestration for detaching models (#66).

Operates inside a model source ``.blend`` session (never inside a prefab workfile): wraps the
selected mesh objects into new top-level asset-marked collections (and wraps the remainder under
the file's stem when transitioning from a whole-file ``.blend``), stamps ``paradise_guid`` on
save, converts, migrates attribution and stages direct placements, extracts without minting seed
prefabs, populates ``MaterialsComponentData.Slots``, and verifies the tree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import bpy

from ..document import asset_guids as doc_asset_guids
from ..document import detach_model as doc_detach
from ..document import project
from ..play import host
from . import asset_guids, store, sync

__all__ = ["DetachResult", "can_offer", "can_poll", "detach"]


@dataclass(frozen=True)
class DetachResult:
    """Summary of a completed detach pass for operator reporting."""

    detached_names: tuple[str, ...]
    placing_prefabs: tuple[str, ...]
    body_name: str


def can_offer(context) -> bool:
    """Cheap draw-rate gate: True only in a saved ``.blend`` that is not a prefab workfile."""
    scene = getattr(context, "scene", None)
    if scene is None or store.read_state(scene) is not None:
        return False
    filepath = bpy.data.filepath
    return bool(filepath) and filepath.lower().endswith(".blend")


def can_poll(context) -> bool:
    """Operator poll: Object Mode in a source ``.blend`` with at least one local unrigged mesh."""
    if not can_offer(context) or getattr(context, "mode", None) != "OBJECT":
        return False
    return any(_is_eligible_mesh(obj) for obj in getattr(context, "selected_objects", ()))


def detach(context, *, collection_name: str = "") -> DetachResult:
    """Detach the selected mesh objects into separate asset-marked models in the same ``.blend``."""
    scene = context.scene
    if store.read_state(scene) is not None:
        raise doc_detach.DetachError(
            "Open the model's source .blend (Edit Source in New Blender) to detach objects; "
            "geometry is edited at source, never inside a prefab document."
        )

    blend_path = os.path.abspath(bpy.data.filepath) if bpy.data.filepath else ""
    layout = project.locate(blend_path) if blend_path else None
    if layout is not None and host.resolve_cli_command(layout.root) is None:
        raise doc_detach.DetachError(
            "Install the Paradise CLI or set its path in addon preferences."
        )

    if context.mode != "OBJECT":
        raise doc_detach.DetachError("Switch to Object Mode before detaching objects.")

    selected = sorted(context.selected_objects, key=lambda o: o.name)
    if not selected:
        raise doc_detach.DetachError("Select one or more mesh objects to detach.")

    for obj in selected:
        if obj.library is not None or getattr(obj.data, "library", None) is not None:
            raise doc_detach.DetachError(
                f"'{obj.name}' is linked from another library and cannot be detached here."
            )
        if obj.type != "MESH" or any(mod.type == "ARMATURE" for mod in obj.modifiers):
            raise doc_detach.DetachError(
                f"'{obj.name}' is not an unrigged static mesh. Select whole static mesh objects."
            )
        if obj.data is None or len(obj.data.polygons) == 0:
            raise doc_detach.DetachError(
                f"'{obj.name}' has no faces to export as a drawable model."
            )

    selected_set = set(selected)
    asset_cols = [
        col for col in bpy.data.collections
        if col.library is None and col.asset_data is not None
    ]

    if not asset_cols:
        body_col = None
        body_asset_guid = None
        body_stem = os.path.splitext(os.path.basename(blend_path))[0]
        remaining_meshes = [
            obj for obj in scene.collection.all_objects
            if obj.library is None and obj.type == "MESH" and obj not in selected_set
            and obj.data is not None and len(obj.data.polygons) > 0
        ]
        if not remaining_meshes:
            raise doc_detach.DetachError(
                "Cannot detach every mesh object from the model — at least one drawable mesh "
                "must remain in the body."
            )
    else:
        owners = {
            col.name: col
            for obj in selected
            for col in asset_cols
            if obj.name in {member.name for member in col.all_objects}
        }
        if len(owners) != 1:
            raise doc_detach.DetachError(
                "Select objects belonging to a single asset collection in this .blend."
            )
        body_col = next(iter(owners.values()))
        if any(
            obj.name not in {member.name for member in body_col.all_objects}
            for obj in selected
        ):
            raise doc_detach.DetachError(
                f"Every selected object must belong to asset collection '{body_col.name}'."
            )
        body_asset_guid = doc_asset_guids.canonical_of(body_col.get(doc_asset_guids.PROPERTY))
        if body_asset_guid is None:
            raise doc_detach.DetachError(
                f"Asset collection '{body_col.name}' has no Paradise GUID; save the .blend first."
            )
        body_stem = body_col.name
        remaining_meshes = [
            obj for obj in body_col.all_objects
            if obj.library is None and obj.type == "MESH" and obj not in selected_set
            and obj.data is not None and len(obj.data.polygons) > 0
        ]
        if not remaining_meshes:
            raise doc_detach.DetachError(
                f"Cannot detach every mesh object from '{body_col.name}' — at least one "
                "drawable mesh must remain."
            )

    grouped = bool(collection_name.strip())
    if grouped:
        groups: list[tuple[str, list]] = [
            (doc_detach.clean_model_name(collection_name), selected)
        ]
    else:
        groups = [(doc_detach.clean_model_name(obj.name), [obj]) for obj in selected]

    existing_col_names = [col.name for col in bpy.data.collections if col.library is None]
    plan = doc_detach.preflight_targets(
        layout=layout,
        blend_path=blend_path,
        body_asset_guid=body_asset_guid,
        existing_collection_names=existing_col_names,
        body_stem=body_stem,
        target_names=[name for name, _members in groups],
    )

    # -- Phase 1: Restructure collections in the .blend, keeping world transforms -----------------
    context.view_layer.update()
    _preserve_world_across_split(scene, selected_set, groups)

    if body_col is None:
        body_col = bpy.data.collections.new(plan.body_stem)
        body_col.asset_mark()
        body_col.instance_offset = (0.0, 0.0, 0.0)
        for root_obj in list(scene.collection.objects):
            if root_obj not in selected_set:
                body_col.objects.link(root_obj)
                scene.collection.objects.unlink(root_obj)
        for child_col in list(scene.collection.children):
            if child_col.library is None and child_col.asset_data is None:
                body_col.children.link(child_col)
                scene.collection.children.unlink(child_col)
        scene.collection.children.link(body_col)

    detached_cols = []
    for target_name, members in groups:
        det_col = bpy.data.collections.new(target_name)
        det_col.asset_mark()
        det_col.instance_offset = (0.0, 0.0, 0.0)
        scene.collection.children.link(det_col)
        for obj in members:
            for holder_col in list(obj.users_collection):
                if holder_col.library is None:
                    holder_col.objects.unlink(obj)
            det_col.objects.link(obj)
        detached_cols.append(det_col)

    asset_guids.stamp(plan.blend_path)
    with sync.suppressed():
        bpy.ops.wm.save_mainfile(filepath=plan.blend_path)

    resolved_body_guid = doc_asset_guids.canonical_of(body_col.get(doc_asset_guids.PROPERTY))
    if resolved_body_guid is None:
        raise doc_detach.DetachError(f"Collection '{body_col.name}' received no Paradise GUID.")
    detached_models: list[tuple[str, str]] = []
    for det_col in detached_cols:
        det_guid = doc_asset_guids.canonical_of(det_col.get(doc_asset_guids.PROPERTY))
        if det_guid is None:
            raise doc_detach.DetachError(f"Collection '{det_col.name}' received no Paradise GUID.")
        detached_models.append((det_col.name, det_guid))

    # -- Phase 2: Convert -> Migrate & Stage -> Extract -> Finalize Slots -> Verify ---------------
    _run_cli(["assets", "convert", plan.blend_path], plan.layout)
    doc_detach.migrate_remainder_attribution(plan, resolved_body_guid, body_col.name)
    detached_meshes, staged = doc_detach.stage_detached_meshes_and_placements(
        plan, detached_models
    )
    _run_cli(["assets", "extract", plan.blend_path], plan.layout)

    for dm in detached_meshes:
        seed_path = os.path.join(plan.directories["prefabs"], f"{dm.name}.prefab")
        if os.path.exists(seed_path):
            raise doc_detach.DetachError(
                f"Unexpected starter prefab minted at {plan.layout.relative(seed_path)}."
            )

    doc_detach.finalize_placements(plan, staged)
    _run_cli(["assets", "verify"], plan.layout)

    return DetachResult(
        detached_names=tuple(dm.name for dm in detached_meshes),
        placing_prefabs=tuple(plan.layout.relative(p) for p in plan.placing_prefabs),
        body_name=body_col.name,
    )


def _is_eligible_mesh(obj) -> bool:
    return (
        obj is not None
        and obj.library is None
        and getattr(obj, "type", None) == "MESH"
        and getattr(obj.data, "library", None) is None
        and not any(mod.type == "ARMATURE" for mod in getattr(obj, "modifiers", ()))
    )


def _preserve_world_across_split(scene, selected_set: set, groups: list[tuple[str, list]]) -> None:
    """Unlink cross-model parent relationships while keeping every object's world matrix intact."""
    # First re-parent any unselected child hanging off a detached object to the nearest
    # unselected ancestor (or None) before we touch the detached objects' own parents.
    for obj in list(scene.collection.all_objects):
        if obj in selected_set or obj.parent not in selected_set:
            continue
        ancestor = obj.parent
        while ancestor is not None and ancestor in selected_set:
            ancestor = ancestor.parent
        world = obj.matrix_world.copy()
        obj.parent = ancestor
        obj.matrix_parent_inverse.identity()
        obj.matrix_world = world

    for _name, members in groups:
        member_set = set(members)
        for obj in members:
            if obj.parent is not None and obj.parent not in member_set:
                world = obj.matrix_world.copy()
                obj.parent = None
                obj.matrix_parent_inverse.identity()
                obj.matrix_world = world


def _run_cli(arguments: list[str], layout: project.ProjectLayout) -> None:
    result = host.run_cli([*arguments, "--project", layout.root], layout.root)
    if result is None:
        raise doc_detach.DetachError("The Paradise CLI could not be started.")
    if result.returncode != 0:
        diagnostic = (result.stderr or result.stdout).strip()
        raise doc_detach.DetachError(diagnostic[-1500:] or f"`paradise {' '.join(arguments)}` failed.")
