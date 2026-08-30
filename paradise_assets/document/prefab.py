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
from .scene import SceneComponent, SceneDocument, SceneDocumentError, SceneObject

__all__ = ["PrefabDocument", "ResolveResult", "mint_child_guid", "resolve", "validate"]


@dataclass
class PrefabDocument:
    """A validated prefab: the document, plus the single root an instance becomes."""

    document: SceneDocument
    root: SceneObject

    @property
    def root_guid(self) -> str:
        return self.root.guid  # type: ignore[return-value]


def validate(document: SceneDocument, source: str) -> PrefabDocument:
    """Applies the prefab rules: exactly one root, no nesting, no carriers.

    The root is INFERRED from the absence of ``meta.Parent`` rather than declared, so nothing can
    disagree with the hierarchy. Exactly one, because an instance places exactly one thing and
    "which of these several is it" has no good answer -- the rule Unity prefabs, Godot's
    PackedScene and Unreal blueprints all settle on.
    """

    def fail(problem: str) -> SceneDocumentError:
        return SceneDocumentError(source, problem)

    if not document.objects:
        raise fail("is a prefab with no objects")

    roots = [o for o in document.objects if o.parent is None]
    if not roots:
        raise fail("is a prefab with no root object (every object declares a parent)")
    if len(roots) > 1:
        names = ", ".join(r.name or r.guid or "?" for r in roots)
        raise fail(
            f"is a prefab with {len(roots)} root objects ({names}); a prefab has exactly one, "
            "because an instance places exactly one thing -- parent the others beneath it"
        )

    for candidate in document.objects:
        if candidate.prefab is not None:
            raise fail("is a prefab that instantiates another prefab, which is not supported yet")
        if candidate.target is not None:
            raise fail("is a prefab containing an override carrier, which only a scene may hold")

    return PrefabDocument(document, roots[0])


@dataclass
class ResolveResult:
    """The flattened document, plus whatever could not be resolved."""

    document: SceneDocument = field(default_factory=SceneDocument)
    errors: list[str] = field(default_factory=list)
    expanded: int = 0


def resolve(document: SceneDocument, prefabs) -> ResolveResult:
    """Expands every instance in ``document``.

    ``prefabs`` maps an :class:`AssetReference` to a :class:`PrefabDocument`, or ``None``.
    """
    result = ResolveResult()

    # Carriers are consumed by the instance they belong to and occupy no slot of their own.
    carriers: dict[tuple[str, str], SceneObject] = {}
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

        prefab = prefabs(candidate.prefab)
        if prefab is None:
            result.errors.append(f"prefab '{candidate.prefab.path}' could not be read")
            continue
        if candidate.guid is None:
            result.errors.append(f"an instance of '{candidate.prefab.path}' has no meta Guid")
            continue

        _expand(candidate, candidate.guid, prefab, carriers, result)
        result.expanded += 1

    return result


def _expand(instance, instance_guid, prefab, carriers, result) -> None:
    minted = {
        member.guid: (instance_guid if member.guid == prefab.root_guid else mint_child_guid(instance_guid, member.guid))
        for member in prefab.document.objects
        if member.guid is not None
    }
    by_guid = prefab.document.by_guid()

    for member in prefab.document.objects:
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
                f"which '{prefab.root.name}' does not contain"
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


def _merge(prefab_object, overrides, is_root, instance_guid, minted, prefab, result) -> SceneObject:
    merged = SceneObject()

    # COMPONENT ORDER: prefab order first, then instance-only additions in instance order.
    for component in prefab_object.components:
        overriding = overrides.component(component.id) if overrides is not None else None
        if overriding is not None and overriding.removed:
            continue
        if overriding is None:
            merged.components.append(component)
            continue
        merged.components.append(
            SceneComponent(
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
                    f"an override removes component '{component.id}', which '{prefab.root.name}' does not have"
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

    component = SceneComponent(well_known.META_ID, well_known.META_TYPE, data)
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
