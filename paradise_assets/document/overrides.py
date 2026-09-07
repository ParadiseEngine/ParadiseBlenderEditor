"""What an instance overrides, and what it would say if it overrode nothing.

The format's override vocabulary lives in :mod:`resolve` (the normative mirror of C#
``PrefabResolver``); this module is the authoring side of it, and the two things authoring needs
that resolution does not:

- **the baseline** -- what the prefab alone says about each displayed object, so the panel can
  show which fields an instance has changed. Computed by resolving the document a second time
  with every override stripped, rather than by teaching the resolver to report provenance: the
  resolver is a mirror of C# and must keep producing exactly what C# produces.
- **the local address** -- a carrier addresses a child as ``(instance guid, prefab-LOCAL guid)``,
  but what a load materializes is ``uuid5(instance, local)``, and uuid5 does not invert. So the
  locals are minted forward, once, and kept.

Imports no ``bpy``: the unit tests are what keep it honest.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from . import resolve, well_known
from .prefab import PrefabComponent, PrefabDocument, PrefabObject

__all__ = [
    "LocalRef",
    "baseline",
    "carrier_for",
    "carriers_of",
    "differs",
    "is_empty",
    "locals_of",
    "new_carrier",
    "strip_instance",
    "strip_overrides",
]


@dataclass(frozen=True)
class LocalRef:
    """How a carrier addresses one resolved child.

    ``own`` is False when the local guid was itself minted while flattening a prefab nested
    inside this one. Such a child IS overridable -- the resolver mints over the flattened
    prefab, so the address resolves -- but it exists in no file, so ``apply`` cannot write the
    override into the prefab document and has to say so.
    """

    instance: str
    local: str
    own: bool


def baseline(document: PrefabDocument, prefabs) -> dict[str, PrefabObject]:
    """Each resolved object as the PREFAB alone would have it, by resolved guid.

    A plain object is its own baseline, which is what makes the panel's diff one code path:
    "different from the baseline" is empty for everything the document authors outright.
    """
    stripped = resolve.resolve(strip_overrides(document), prefabs)
    return {entry.guid: entry for entry in stripped.document.objects if entry.guid is not None}


def strip_overrides(document: PrefabDocument) -> PrefabDocument:
    """``document`` with every instance reduced to ``meta`` + ``transform`` and every carrier
    gone -- the shape :func:`~.extract.extract` leaves behind, which is the definition of an
    instance that overrides nothing."""
    bare = PrefabDocument()
    for entry in document.objects:
        if entry.target is not None:
            continue
        if entry.prefab is None:
            bare.objects.append(entry)
            continue
        kept = PrefabObject(prefab=entry.prefab)
        for component in entry.components:
            if component.id in (well_known.META_ID, well_known.TRANSFORM_ID):
                kept.components.append(copy.deepcopy(component))
        bare.objects.append(kept)
    return bare


def strip_instance(document: PrefabDocument, instance_guid: str) -> PrefabDocument:
    """``document`` with ONE instance's overrides taken off: its entry back to ``meta`` and
    ``transform``, its carriers gone. Every other object keeps its identity, so nothing that
    named the instance or one of its children stops resolving.

    This is both halves of "the override is no longer here": what ``apply`` leaves behind once
    the prefab has been told, and the whole of "revert this instance to its prefab".
    """
    stripped = PrefabDocument()
    for entry in document.objects:
        if entry.target is not None and entry.parent == instance_guid:
            continue
        if entry.guid != instance_guid or entry.prefab is None:
            stripped.objects.append(entry)
            continue
        bare = PrefabObject(prefab=entry.prefab)
        for component in entry.components:
            if component.id in (well_known.META_ID, well_known.TRANSFORM_ID):
                bare.components.append(copy.deepcopy(component))
        stripped.objects.append(bare)
    return stripped


def locals_of(document: PrefabDocument, prefabs) -> dict[str, LocalRef]:
    """``{resolved child guid: LocalRef}`` for every instance in ``document``.

    The prefab is FLATTENED first, exactly as ``resolve._expand`` sees it, so a child that came
    out of a prefab nested inside this one is addressed by the guid the resolver minted for it
    there -- the same value a carrier has to spell.
    """
    found: dict[str, LocalRef] = {}
    # By path, not per instance: a level places one prop 122 times (ShiningPie), and flattening
    # it once per placement is 122 walks of the same tree.
    flattened: dict[str, tuple[PrefabDocument, set[str]] | None] = {}

    for entry in document.objects:
        if entry.prefab is None or entry.guid is None or entry.target is not None:
            continue
        if entry.prefab.path not in flattened:
            flattened[entry.prefab.path] = _flatten_once(entry.prefab, prefabs)
        prepared = flattened[entry.prefab.path]
        if prepared is None:
            continue

        flat, own = prepared
        for member in flat.objects:
            if member.guid is None or member.guid == flat.root_guid:
                continue
            minted = resolve.mint_child_guid(entry.guid, member.guid)
            found[minted] = LocalRef(entry.guid, member.guid, member.guid in own)

    return found


def _flatten_once(reference, prefabs):
    """``(flattened prefab, the guids its OWN file declares)``, or ``None`` when it cannot be
    read. The second half is what separates a child ``apply`` can write back from one minted
    inside a nested prefab, which exists in no file."""
    flat = resolve.flatten(reference, prefabs)
    if flat is None:
        return None
    raw = prefabs(reference)
    return flat, set(raw.by_guid()) if raw is not None else set()


def carriers_of(document: PrefabDocument, instance_guid: str) -> dict[str, PrefabObject]:
    """This instance's override carriers, by the prefab-local guid each addresses."""
    return {
        entry.target: entry
        for entry in document.objects
        if entry.target is not None and entry.parent == instance_guid
    }


def carrier_for(document: PrefabDocument, instance_guid: str, local: str) -> PrefabObject | None:
    return carriers_of(document, instance_guid).get(local)


def new_carrier(instance_guid: str, local: str) -> PrefabObject:
    """An empty carrier. It gets no ``Guid``: a carrier addresses a child rather than being one,
    and the reader exempts it from the document's identity map for exactly that reason."""
    return PrefabObject(
        components=[
            PrefabComponent(
                well_known.META_ID,
                well_known.META_TYPE,
                {well_known.PARENT: instance_guid, well_known.TARGET: local},
            )
        ]
    )


def is_empty(carrier: PrefabObject) -> bool:
    """Whether this carrier now says nothing, and so should not be written. A carrier that only
    addresses a child overrides nothing about it, and one left in the file is a row that the
    next reader has to prove harmless."""
    if carrier.dropped:
        return False
    for component in carrier.components:
        if component.id != well_known.META_ID:
            return False
        if any(not well_known.is_meta_field(key) for key in component.data):
            return False
    return True


def differs(base: dict, shown: dict) -> set[str]:
    """Which TOP-LEVEL fields of one component payload the instance has changed.

    Top-level only, because :func:`resolve._merge_data` is shallow: an override replaces a whole
    sub-table rather than merging into it, so the field an author overrode IS the top-level one.
    """
    return {key for key in set(base) | set(shown) if base.get(key) != shown.get(key)}
