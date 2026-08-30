"""Prefabs and their resolution -- the mirror of C# ``PrefabDocument`` and ``PrefabResolver``.

Prefabs are an AUTHORING concept: the build flattens an instance into plain objects, so nothing
downstream knows they exist. This addon resolves them for the same reason the build does -- to
show you the objects a scene actually contains.

**Everything order- and identity-related here is normative.** The C# resolver and this one must
produce the same documents, so the rules are written down rather than left to whatever each
implementation's loops happen to do:

* object order -- resolved children follow their instance immediately, in prefab document order;
* component order -- prefab order, then instance-only additions in instance order;
* child identity -- ``uuid5(instance guid, prefab-local guid AS TEXT)``. Text, because .NET's
  ``Guid.ToByteArray`` is mixed-endian and Python's ``UUID.bytes`` is big-endian: hashing raw
  bytes would mint different identities in each language, and nothing would notice until two
  tools disagreed about a document.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from . import well_known
from .prefab import PrefabComponent, PrefabDocument, PrefabDocumentError, PrefabObject

__all__ = ["ResolveResult", "mint_child_guid", "resolve"]

#: How deep instances may nest before resolution gives up.
#:
#: The cycle check already catches a prefab that reaches itself. This catches the other runaway --
#: a chain that is acyclic but absurd -- with a number no real asset approaches.
MAX_NESTING_DEPTH = 32


@dataclass
class ResolveResult:
    """The flattened document, plus whatever could not be resolved."""

    document: PrefabDocument = field(default_factory=PrefabDocument)
    errors: list[str] = field(default_factory=list)
    expanded: int = 0


def resolve(document: PrefabDocument, prefabs) -> ResolveResult:
    """Expands every instance in ``document``, however deeply they nest.

    ``prefabs`` maps an :class:`AssetReference` to a :class:`PrefabDocument`, or ``None``.
    """
    result = ResolveResult()
    result.expanded = _expand_document(document, prefabs, result, [], {}, 0)
    return result


def _expand_document(document, prefabs, result, stack, cache, depth) -> int:
    """Expands one document's instances into ``result``; returns how many THIS document had.

    Deliberately not cumulative: a nested prefab is flattened once and reused by every instance of
    it, so adding its internal expansions would report a number that grows with caching rather
    than with the document.
    """
    expanded = 0

    # Carriers are consumed by the instance they belong to and occupy no slot of their own.
    carriers: dict[tuple[str, str], PrefabObject] = {}
    for candidate in document.objects:
        if candidate.target is None:
            continue
        if candidate.parent is None:
            result.errors.append(
                f"an override carrier targeting '{candidate.target}' has no Parent naming the instance it belongs to"
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

        # FLATTEN THE PREFAB FIRST, then expand it. In this order nesting falls out: _expand only
        # ever sees a prefab with no instances left in it, so it needs no notion of depth.
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
    """A prefab with its own instances already expanded, or ``None`` if it could not resolve.

    Cached by path: a flattened prefab does not depend on who instantiates it -- minting happens at
    expansion time from the instance's guid -- so one file is flattened once however many instances
    it has.

    The stack is the cycle detector. A prefab that reaches itself would otherwise recurse until the
    interpreter gave up, and "box -> rail -> box" tells an author what to delete where a
    RecursionError does not.
    """
    key = reference.path
    lowered = [entry.lower() for entry in stack]
    if key.lower() in lowered:
        chain = " -> ".join(stack[lowered.index(key.lower()):] + [key])
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
        member.guid: (instance_guid if member.guid == prefab.root_guid else mint_child_guid(instance_guid, member.guid))
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
                    f"an override removes component '{component.id}', which '{prefab.root().name}' does not have"
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

    # The ROOT's parent comes from the instance -- that is how a prefab is placed under something.
    # A child's comes from the prefab, remapped to the minted identity.
    if is_root:
        parent = overrides.parent if overrides is not None else None
    else:
        parent = minted.get(prefab_object.parent) if prefab_object.parent else None
    if parent:
        data[well_known.PARENT] = parent

    if existing is not None:
        for key, value in existing.data.items():
            if key in (well_known.GUID, well_known.NAME, well_known.PARENT, well_known.TARGET, well_known.DROPPED):
                continue
            data[key] = value

    component = PrefabComponent(well_known.META_ID, well_known.META_TYPE, data)
    if index >= 0:
        merged.components[index] = component
    else:
        merged.components.insert(0, component)


def _merge_data(prefab_data: dict, override_data: dict) -> dict:
    """Field by field: the instance's value wins, everything else is inherited.

    Fields the prefab's component does not declare are ADDED, not refused. Whether a field belongs
    to the component at all is a SCHEMA question -- refusing here would forbid an instance setting
    ``meta.Parent``, since a prefab root deliberately has none, and that is the commonest edit
    there is.
    """
    merged = {key: override_data.get(key, value) for key, value in prefab_data.items()}
    for key, value in override_data.items():
        if key not in merged:
            merged[key] = value
    return merged


def mint_child_guid(instance: str, prefab_local: str) -> str:
    """A resolved child's scene identity: ``uuid5(instance, prefab-local guid as text)``.

    Deterministic, so a scene resolves to the same identities on every machine. The namespace is
    the INSTANCE, so twenty instances of one prefab give twenty distinct sets of children with no
    bookkeeping in the document.
    """
    return str(uuid.uuid5(uuid.UUID(instance), prefab_local))
