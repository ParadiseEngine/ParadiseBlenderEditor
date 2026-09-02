"""Prefab resolution, mirroring C# ``PrefabResolver``. Order and identity are NORMATIVE (both
resolvers must produce the same document): children follow their instance in prefab order;
components in prefab order then instance additions; child identity is ``uuid5(instance guid,
prefab-local guid AS TEXT)``, text because .NET's ``Guid.ToByteArray`` is mixed-endian. KNOWN
GAP (#30): C# hashes the canonical lowercase text; this hashes the raw text.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from . import well_known
from .prefab import PrefabComponent, PrefabDocument, PrefabObject

__all__ = ["ResolveResult", "mint_child_guid", "resolve"]

#: Catches an acyclic but absurd chain; the cycle check catches the rest.
MAX_NESTING_DEPTH = 32


@dataclass
class ResolveResult:
    """The flattened document, plus whatever could not be resolved."""

    document: PrefabDocument = field(default_factory=PrefabDocument)
    errors: list[str] = field(default_factory=list)
    expanded: int = 0


def resolve(document: PrefabDocument, prefabs) -> ResolveResult:
    """Expands every instance in ``document``, however deeply nested."""
    result = ResolveResult()
    result.expanded = _expand_document(document, prefabs, result, [], {}, 0)
    return result


def _expand_document(document, prefabs, result, stack, cache, depth) -> int:
    """Expands one document's instances into ``result``; the count is not cumulative, or it
    would grow with caching rather than with the document."""
    expanded = 0

    # Carriers are consumed by the instance they belong to and occupy no slot of their own.
    carriers: dict[tuple[str, str], PrefabObject] = {}
    for candidate in document.objects:
        if candidate.target is None:
            continue
        if candidate.parent is None:
            result.errors.append(
                f"an override carrier targeting '{candidate.target}' "
                "has no Parent naming the instance it belongs to"
            )
            continue
        key = (candidate.parent, candidate.target)
        if key in carriers:
            result.errors.append(f"two override carriers address '{candidate.target}' on the same instance")
            continue
        carriers[key] = candidate

    for candidate in document.objects:
        if candidate.target is not None:
            continue  # consumed above

        if candidate.prefab is None:
            result.document.objects.append(candidate)
            continue

        # Flatten first: _expand then never sees a prefab with instances left in it.
        prefab = _flatten(candidate.prefab, prefabs, result, stack, cache, depth)
        if prefab is None:
            continue  # the failure is already recorded
        if candidate.guid is None:
            result.errors.append(f"an instance of '{candidate.prefab.path}' has no meta Guid")
            continue

        _expand(candidate, candidate.guid, prefab, carriers, result)
        expanded += 1

    return expanded


def _flatten(reference, prefabs, result, stack, cache, depth):
    """A prefab with its own instances expanded, cached by path, or ``None``. The stack is the
    cycle detector: "box -> rail -> box" tells an author what to delete where a RecursionError
    does not."""
    key = reference.path
    lowered = [entry.lower() for entry in stack]
    if key.lower() in lowered:
        chain = " -> ".join([*stack[lowered.index(key.lower()):], key])
        result.errors.append(f"prefabs form a cycle: {chain}")
        return None

    if depth >= MAX_NESTING_DEPTH:
        result.errors.append(
            f"prefab '{key}' nests more than {MAX_NESTING_DEPTH} deep; this is almost certainly a mistake"
        )
        return None

    if key in cache:
        return cache[key]

    raw = prefabs(reference)
    if raw is None:
        result.errors.append(f"prefab '{key}' could not be read")
        cache[key] = None
        return None

    flat = PrefabDocument()
    nested = ResolveResult(document=flat, errors=result.errors)

    stack.append(key)
    _expand_document(raw, prefabs, nested, stack, cache, depth + 1)
    stack.pop()

    cache[key] = flat
    return flat


def _expand(instance, instance_guid, prefab, carriers, result) -> None:
    minted = {
        member.guid: (
            instance_guid if member.guid == prefab.root_guid else mint_child_guid(instance_guid, member.guid)
        )
        for member in prefab.objects
        if member.guid is not None
    }
    by_guid = prefab.by_guid()

    for member in prefab.objects:
        if member.guid is None:
            continue

        is_root = member.guid == prefab.root_guid
        overrides = instance if is_root else carriers.get((instance_guid, member.guid))

        if overrides is not None and overrides.dropped:
            continue
        if not is_root and _drops_ancestor(member, by_guid, instance_guid, carriers):
            continue

        result.document.objects.append(
            _merge(member, overrides, is_root, instance_guid, minted, prefab, result)
        )

    for (owner, target) in carriers:
        if owner == instance_guid and target not in minted:
            result.errors.append(
                f"an override on instance '{instance_guid}' targets '{target}', "
                f"which '{prefab.root().name}' does not contain"
            )


def _drops_ancestor(member, by_guid, instance_guid, carriers) -> bool:
    parent = member.parent
    while parent is not None:
        carrier = carriers.get((instance_guid, parent))
        if carrier is not None and carrier.dropped:
            return True
        owner = by_guid.get(parent)
        parent = owner.parent if owner is not None else None
    return False


def _merge(prefab_object, overrides, is_root, instance_guid, minted, prefab, result) -> PrefabObject:
    merged = PrefabObject()

    # COMPONENT ORDER: prefab order first, then instance-only additions in instance order.
    for component in prefab_object.components:
        overriding = overrides.component(component.id) if overrides is not None else None
        if overriding is not None and overriding.removed:
            continue
        if overriding is None:
            merged.components.append(component)
            continue
        merged.components.append(
            PrefabComponent(
                component.id,
                component.type or overriding.type,
                _merge_data(component.data, overriding.data),
            )
        )

    if overrides is not None:
        for component in overrides.components:
            if prefab_object.component(component.id) is not None:
                continue
            if component.removed:
                result.errors.append(
                    f"an override removes component '{component.id}', "
                    f"which '{prefab.root().name}' does not have"
                )
                continue
            merged.components.append(component)

    _rewrite_meta(merged, prefab_object, is_root, instance_guid, minted, overrides)
    return merged


def _rewrite_meta(merged, prefab_object, is_root, instance_guid, minted, overrides) -> None:
    """Minted identity, remapped parent, and none of the carrier-only fields."""
    index = next((i for i, c in enumerate(merged.components) if c.id == well_known.META_ID), -1)
    existing = merged.components[index] if index >= 0 else None

    data: dict = {well_known.GUID: instance_guid if is_root else minted[prefab_object.guid]}

    name = (existing.data.get(well_known.NAME) if existing else None) or prefab_object.name
    if name is not None:
        data[well_known.NAME] = name

    # The root's parent comes from the instance; a child's from the prefab, remapped.
    if is_root:
        parent = overrides.parent if overrides is not None else None
    else:
        parent = minted.get(prefab_object.parent) if prefab_object.parent else None
    if parent:
        data[well_known.PARENT] = parent

    if existing is not None:
        for key, value in existing.data.items():
            if well_known.is_meta_field(key):
                continue
            data[key] = value

    component = PrefabComponent(well_known.META_ID, well_known.META_TYPE, data)
    if index >= 0:
        merged.components[index] = component
    else:
        merged.components.insert(0, component)


def _merge_data(prefab_data: dict, override_data: dict) -> dict:
    """Instance value wins per field; undeclared fields are ADDED, or an instance could not set
    ``meta.Parent`` on a root that deliberately has none."""
    merged = {key: override_data.get(key, value) for key, value in prefab_data.items()}
    for key, value in override_data.items():
        if key not in merged:
            merged[key] = value
    return merged


def mint_child_guid(instance: str, prefab_local: str) -> str:
    """``uuid5(instance, prefab-local guid as text)``: deterministic, and distinct per instance."""
    return str(uuid.uuid5(uuid.UUID(instance), prefab_local))
