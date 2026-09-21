"""Host-authored transform fields, displayed as world-placement Empty handles.

The handles have no document identity. Their owner records which slots were materialized, so
deleting a handle clears that slot without turning a previously absent value into an edit.
Only a moved or explicitly assigned handle is baked; an inherited field stays inherited until
the author changes it.
"""

from __future__ import annotations

import copy
import json
import math

import bpy
from mathutils import Matrix

from .. import edits
from ..document import axes
from ..document.prefab import PrefabComponent
from . import store

HELPER_KEY = "paradise_transform_helper"
SLOTS_KEY = "paradise_transform_slots"
LINKS_KEY = "paradise_transform_links"
_EPSILON = 1e-6


def is_helper(obj) -> bool:
    return obj is not None and HELPER_KEY in obj


def owner_of(obj):
    tag = _tag(obj)
    if tag is None:
        return None
    if obj.parent is not None and store.guid_of(obj.parent) == tag["owner"]:
        return obj.parent
    for scene in obj.users_scene:
        owner = store.object_with_guid(scene, tag["owner"])
        if owner is not None:
            return owner
    return None


def editable(obj) -> bool:
    address = store.local_of(obj) if obj is not None else None
    return obj is not None and store.guid_of(obj) is not None and (address is None or address[2])


def helper_for(obj, component_id: str, path: str):
    """Read an owner-local Blender ID link; panel redraws never scan the scene.

    ID links survive renames and become null on deletion. Load, assignment and successful save
    synchronize them alongside the slot list; the full helper scan remains on explicit edits
    and save, where it also detects duplicated handles.
    """
    links = obj.get(LINKS_KEY) if obj is not None else None
    if links is None:
        return None
    for index, slot in enumerate(_slots(obj)):
        if slot["component"].lower() != component_id.lower() or slot["field"] != path:
            continue
        empty = links.get(str(index))
        tag = _tag(empty)
        if (tag is not None and tag["owner"] == store.guid_of(obj)
                and tag["component"].lower() == component_id.lower() and tag["field"] == path):
            return empty
    return None


def _helpers(obj, component_id: str, path: str) -> list:
    if obj is None:
        return []
    owner = store.guid_of(obj)
    candidates = {child for scene in obj.users_scene for child in scene.collection.all_objects}
    return [child for child in candidates
            if (tag := _tag(child)) is not None and tag["owner"] == owner
            and tag["component"].lower() == component_id.lower() and tag["field"] == path]


def materialize(obj, components: list, vocabulary) -> int:
    made = 0
    for component in components:
        schema = vocabulary.describe(component)
        if schema is None:
            continue
        data = component.get("data", {})
        for path, field in _fields(schema.fields, data):
            value = edits.read_path(data, path)
            placement = _placement(field, value)
            if placement is None:
                continue
            _create(obj, component["id"], path, field, placement)
            made += 1
    return made


def assign(obj, component_id: str, path: str, field, matrix):
    """Place this field's handle at an explicitly chosen Blender world matrix."""
    if not editable(obj):
        raise ValueError("Open the prefab that authors this component to edit it")
    empty = helper_for(obj, component_id, path)
    if empty is None:
        empty = _create(obj, component_id, path, field, matrix)
    else:
        if empty.parent is not None:
            empty.matrix_parent_inverse = empty.parent.matrix_world.inverted_safe()
        empty.matrix_world = matrix.copy()
    tag = _tag(empty)
    tag["assigned"] = True
    empty[HELPER_KEY] = json.dumps(tag)
    return empty


def clear(obj, component_id: str, path: str) -> None:
    for empty in _helpers(obj, component_id, path):
        bpy.data.objects.remove(empty, do_unlink=True)


def bake(obj, entry, vocabulary) -> int:
    """Apply changed handles to an object's own entry or a prefab-child override carrier."""
    if not editable(obj):
        # A nested helper is a preview of its prefab's payload. Ancestor movement also moves
        # the preview, so treating that as an edit would reject legitimate parent transforms.
        # The successful-save refresh restores/recreates the preview from the resolved value.
        return 0
    changed = 0
    visible = edits.visible_components(store.component_json(obj), edits.read_structure(obj))
    by_id = {str(c.get("id", "")).lower(): c for c in visible}
    reverted = edits.reverted_paths(obj)
    for slot in _slots(obj):
        component_id, path = slot["component"], slot["field"]
        source = by_id.get(component_id.lower())
        if source is None:
            continue
        schema = vocabulary.describe(source)
        field = schema.resolve(path) if schema is not None else None
        if field is None or field.authored_by != "transform":
            continue
        if any(not p or path == p or path.startswith(p + "/")
               for p in reverted.get(component_id, [])):
            continue
        found = _helpers(obj, component_id, path)
        if len(found) > 1:
            raise ValueError(f"{obj.name}: {path} has duplicate transform handles; delete the copy")
        empty = found[0] if found else None
        if empty is not None and not _moved(empty):
            continue
        component = entry.component(component_id)
        shown = copy.deepcopy(source.get("data", {}))
        if component is not None:
            shown.update(copy.deepcopy(component.data))
        original = edits.read_path(shown, path)
        inherited = _inherited(obj, component_id, path)
        if empty is None and not inherited and component is not None:
            changed += int(edits.drop_path(component.data, path))
            continue
        if empty is None and not inherited:
            continue
        value = {} if empty is None else _baked(field, original, empty.matrix_world)
        if component is None:
            component = PrefabComponent(component_id, source.get("type"), {})
            entry.components.append(component)
        # Prefab overrides replace a complete top-level field. Preserve its other members.
        top = path.split("/")[0]
        if "/" in path and top not in component.data:
            component.data[top] = copy.deepcopy(shown.get(top, {}))
        edits.write_path(component.data, path, value)
        changed += 1
    return changed


def refresh(scene, vocabulary) -> None:
    """Refresh handle baselines after a successful save, including explicit prefab reverts."""
    # Only retain document owners: clearing one component deletes its helpers during this walk,
    # invalidating any Blender object wrappers for those helpers in a mixed snapshot.
    documents = {}
    helpers_by_owner = {}
    for obj in scene.collection.all_objects:
        if (guid := store.guid_of(obj)) is not None:
            documents[guid] = obj
        elif (tag := _tag(obj)) is not None:
            helpers_by_owner.setdefault(tag["owner"], []).append((obj, tag))
    for owner, helpers in helpers_by_owner.items():
        if owner not in documents:
            for empty, _tagged in helpers:
                bpy.data.objects.remove(empty, do_unlink=True)
    for owner, obj in documents.items():
        helpers = helpers_by_owner.get(owner, [])
        by_field = {(tag["component"].lower(), tag["field"]): empty for empty, tag in helpers}
        expected = []
        links = {}
        kept = set()
        for component in store.component_json(obj):
            schema = vocabulary.describe(component)
            if schema is None:
                continue
            data = component.get("data", {})
            for path, field in _fields(schema.fields, data):
                matrix = _placement(field, edits.read_path(data, path))
                if matrix is None:
                    continue
                cid = component["id"]
                expected.append({"component": cid, "field": path})
                empty = by_field.get((cid.lower(), path))
                if empty is None:
                    empty = _create(obj, cid, path, field, matrix)
                else:
                    if empty.parent is not None:
                        empty.matrix_parent_inverse = empty.parent.matrix_world.inverted_safe()
                    empty.matrix_world = matrix
                    _lock_channels(empty, obj, field)
                links[str(len(expected) - 1)] = empty
                kept.add(empty.name)
                tag = _tag(empty)
                tag["baseline"] = _matrix_rows(matrix)
                tag.pop("assigned", None)
                empty[HELPER_KEY] = json.dumps(tag)
        for empty, _tagged in helpers:
            if empty.name not in kept:
                bpy.data.objects.remove(empty, do_unlink=True)
        if expected or SLOTS_KEY in obj:
            obj[SLOTS_KEY] = json.dumps(expected)
            obj[LINKS_KEY] = links


def _fields(fields, data, prefix=""):
    for field in fields:
        path = f"{prefix}/{field.name}" if prefix else field.name
        value = data.get(field.name) if isinstance(data, dict) else None
        if field.authored_by == "transform":
            yield path, field
        elif field.type == "object" and isinstance(value, dict):
            yield from _fields(field.fields, value, path)


def _placement(field, value):
    if not isinstance(value, dict) or not value:
        return None
    names = {child.name for child in field.fields}
    position = value.get("Position", (0, 0, 0))
    rotation = value.get("Rotation", (0, 0, 0, 1))
    scale = value.get("Scale", (0, 0, 0) if "Scale" in names else (1, 1, 1))
    try:
        if "Yaw" in names and "Rotation" not in names:
            yaw = float(value.get("Yaw", 0))
            rotation = (0, math.sin(yaw / 2), 0, math.cos(yaw / 2))
        if any(len(v) != n or not all(math.isfinite(float(x)) for x in v)
               for v, n in ((position, 3), (rotation, 4), (scale, 3))):
            return None
        position, rotation, scale = (tuple(float(x) for x in v) for v in (position, rotation, scale))
        if any(float(v) == 0 for v in scale) or sum(float(v) ** 2 for v in rotation) == 0:
            return None
        return Matrix(axes.to_blender(axes.trs_to_matrix(position, rotation, scale)))
    except (TypeError, ValueError):
        return None


def _baked(field, original, world) -> dict:
    data = copy.deepcopy(original) if isinstance(original, dict) else {}
    # Conjugate the complete world matrix BEFORE decomposing: scaled parents permute axes too.
    position, rotation, scale = Matrix(axes.to_document(_matrix_rows(world))).decompose()
    rotation.normalize()
    if rotation.w < 0:
        rotation.negate()
    values = {"Position": list(position), "Rotation": [rotation.x, rotation.y, rotation.z, rotation.w],
              "Scale": list(scale)}
    values["Yaw"] = math.atan2(2 * (rotation.w * rotation.y + rotation.x * rotation.z),
                              1 - 2 * (rotation.y ** 2 + rotation.z ** 2))
    for child in field.fields:
        if child.name not in values:
            continue
        computed = values[child.name]
        if not _same(data.get(child.name), computed, child.name == "Rotation"):
            data[child.name] = computed
    return data


def _same(held, computed, quaternion=False) -> bool:
    if isinstance(computed, (int, float)):
        return isinstance(held, (int, float)) and abs(held - computed) <= _EPSILON * max(abs(held), 1)
    if not isinstance(held, (tuple, list)) or len(held) != len(computed):
        return False
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in held):
        return False
    if quaternion:
        length = math.sqrt(sum(v * v for v in held))
        dot = sum(a * b for a, b in zip(held, computed, strict=True))
        return length > 0 and abs(1 - abs(dot / length)) <= _EPSILON
    return all(abs(a - b) <= _EPSILON * max(abs(a), 1) for a, b in zip(held, computed, strict=True))


def _inherited(obj, component_id, path) -> bool:
    if store.prefab_of(obj) is None and not store.is_derived(obj):
        return False
    return any(str(c.get("id", "")).lower() == component_id.lower()
               and edits.read_path(c.get("data", {}), path) is not None for c in store.base_json(obj))


def _create(owner, component_id, path, field, matrix):
    empty = bpy.data.objects.new(f"{owner.name}.{path.replace('/', '.')}", None)
    empty.empty_display_type = "ARROWS"
    empty.empty_display_size = 0.75
    empty.show_in_front = True
    for collection in owner.users_collection:
        collection.objects.link(empty)
    empty.parent = owner
    # Cancel the owner's current world matrix so a rotated handle under nonuniform parent
    # scale does not acquire shear just by loading. Later parent edits still move the handle.
    empty.matrix_parent_inverse = owner.matrix_world.inverted_safe()
    empty.rotation_mode = "QUATERNION"
    empty.matrix_world = matrix.copy()
    _lock_channels(empty, owner, field)
    empty[HELPER_KEY] = json.dumps({"owner": store.guid_of(owner), "component": component_id,
                                    "field": path, "baseline": _matrix_rows(matrix)})
    slots = _slots(owner)
    slot = {"component": component_id, "field": path}
    if slot not in slots:
        slots.append(slot)
        owner[SLOTS_KEY] = json.dumps(slots)
    if LINKS_KEY not in owner:
        owner[LINKS_KEY] = {}
    owner[LINKS_KEY][str(slots.index(slot))] = empty
    return empty


def _lock_channels(empty, owner, field):
    names = {child.name for child in field.fields}
    read_only = not editable(owner)
    empty.lock_location = (read_only or "Position" not in names,) * 3
    empty.lock_rotation = (read_only or ("Rotation" not in names and "Yaw" not in names),) * 3
    empty.lock_scale = (read_only or "Scale" not in names,) * 3
    empty.lock_rotation_w = read_only
    empty.lock_rotations_4d = read_only


def _moved(empty) -> bool:
    tag = _tag(empty)
    return tag.get("assigned", False) or any(
        not _same(a, b) for a, b in zip(
            tag.get("baseline", []), _matrix_rows(empty.matrix_world), strict=True))


def _matrix_rows(matrix) -> list:
    return [[float(v) for v in row] for row in matrix]


def _slots(obj) -> list:
    try:
        value = json.loads(obj.get(SLOTS_KEY, "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [slot for slot in value if isinstance(slot, dict)
            and isinstance(slot.get("component"), str) and isinstance(slot.get("field"), str)]


def _tag(obj):
    if not is_helper(obj):
        return None
    try:
        tag = json.loads(obj[HELPER_KEY])
    except (TypeError, json.JSONDecodeError):
        return None
    return tag if isinstance(tag, dict) and all(
        isinstance(tag.get(key), str) for key in ("owner", "component", "field")) else None
