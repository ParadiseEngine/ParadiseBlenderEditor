"""Prefab resolution, mirroring C# ``PrefabResolver``. Order and identity are NORMATIVE (both
resolvers must produce the same document): children follow their instance in prefab order;
components in prefab order then instance additions; child identity is ``uuid5(instance guid,
prefab-local guid AS CANONICAL TEXT)``, text because .NET's ``Guid.ToByteArray`` is mixed-endian
and canonical because the file's spelling is not part of the identity (#30). A prefab payload
value naming one of the prefab's own members by its local guid is rewritten to that member's
resolved guid (:func:`_remap_references`, #70).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from . import canonical_toml, well_known
from . import guid as document_guid
from .prefab import PrefabComponent, PrefabDocument, PrefabObject

__all__ = ["ResolveResult", "flatten", "mint_child_guid", "remap_guids", "resolve"]

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


def flatten(reference, prefabs) -> PrefabDocument | None:
    """One prefab with its own instances expanded, as ``_expand`` sees it. Public because
    authoring has to mint the same child identities the resolver does, and those are minted over
    the FLATTENED prefab -- a child of a prefab nested inside this one included."""
    return _flatten(reference, prefabs, ResolveResult(), [], {}, 0)


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
    # By VALUE, once per instance: a payload may spell a guid in any accepted form.
    references = {document_guid.parse(local): resolved for local, resolved in minted.items()}
    references.pop(None, None)

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
            _merge(member, overrides, is_root, instance_guid, minted, references, prefab, result)
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


def _merge(
    prefab_object, overrides, is_root, instance_guid, minted, references, prefab, result
) -> PrefabObject:
    merged = PrefabObject()

    # COMPONENT ORDER: prefab order first, then instance-only additions in instance order.
    for prefab_component in prefab_object.components:
        overriding = overrides.component(prefab_component.id) if overrides is not None else None
        if overriding is not None and overriding.removed:
            continue
        # The PREFAB's values are remapped before the merge, the instance's never: an override is
        # spelled in the document that holds the instance, where the resolved guids live.
        component = _remap_references(prefab_component, references)
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

    # `is None`, not `or`: an instance that overrides the name to "" means "", as in C#.
    name = existing.data.get(well_known.NAME) if existing is not None else None
    if not isinstance(name, str):
        name = prefab_object.name
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


def _remap_references(component: PrefabComponent, references: dict) -> PrefabComponent:
    """``component`` with every payload string naming a member of the prefab by its prefab-LOCAL
    guid rewritten to that member's resolved guid, or ``component`` itself when none does.

    A prefab that names one of its own members (an entity reference, a host id) can only spell
    the member's local guid, which no object answers to once the prefab is placed: the member
    becomes ``uuid5(instance, local)``, the root the instance guid. Remapping here makes such a
    reference name THIS instance's member, and it composes through nesting: flattening rewrites
    an inner prefab's references to the flattened locals, which the outer expansion rewrites
    again (ParadiseBlenderEditor#70).

    NORMATIVE, mirroring C# ``PrefabResolver.RemapReferences``. The resolver has no schema, so
    the rule is by value: a string that parses as a document guid (:func:`guid.parse`) and is the
    local guid of a member of the flattened prefab, anywhere in a payload -- nested tables and
    lists included -- is written as the member's resolved guid in canonical form. Everything else
    is untouched, its spelling included. ``meta`` is skipped (:func:`_rewrite_meta` owns identity
    and parenting), and so is a table shaped like an asset reference, which names an asset, never
    an object.
    """
    if component.id == well_known.META_ID or not component.data:
        return component
    data = remap_guids(component.data, references)
    if data is component.data:
        return component
    return PrefabComponent(component.id, component.type, data, component.removed)


def remap_guids(value, references: dict):
    """``value`` with every guid string ``references`` knows rewritten, or ``value`` itself when
    nothing in it changed. ``references`` maps a guid, as a ``uuid.UUID``, to the guid text it
    becomes. Walks tables and lists; a table shaped like an asset reference is left alone.
    Public because :mod:`apply` runs the same rule backwards."""
    if isinstance(value, str):
        parsed = document_guid.parse(value)
        resolved = references.get(parsed) if parsed is not None else None
        return document_guid.canonical(resolved) if resolved is not None else value
    if isinstance(value, dict):
        if canonical_toml.is_written_inline(value):
            return value
        items = {key: remap_guids(item, references) for key, item in value.items()}
        if all(items[key] is item for key, item in value.items()):
            return value
        remapped = type(value)()   # keeps an InlineTable inline
        remapped.update(items)
        return remapped
    if isinstance(value, list):
        items = [remap_guids(item, references) for item in value]
        if all(new is old for new, old in zip(items, value, strict=True)):
            return value
        return items
    return value


def _merge_data(prefab_data: dict, override_data: dict) -> dict:
    """Instance value wins per field; undeclared fields are ADDED, or an instance could not set
    ``meta.Parent`` on a root that deliberately has none."""
    merged = {key: override_data.get(key, value) for key, value in prefab_data.items()}
    for key, value in override_data.items():
        if key not in merged:
            merged[key] = value
    return merged


def mint_child_guid(instance: str, prefab_local: str) -> str:
    """``uuid5(instance, prefab-local guid as canonical text)``: deterministic, distinct per
    instance, and the same value C# ``PrefabResolver.MintChildGuid`` produces -- which hashes
    ``DocumentGuid.Format(prefabLocal)``, never the spelling a file happened to use."""
    return str(uuid.uuid5(document_guid.parse(instance), document_guid.canonical(prefab_local)))
