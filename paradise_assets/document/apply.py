"""Pushing an instance's overrides back into the prefab they shadow.

An override is a local answer to a question the prefab got wrong, and applying it is the author
saying "the prefab was wrong". So this writes the override INTO the prefab document and takes it
off the instance, leaving an instance that overrides nothing -- the same shape
:mod:`extract` leaves behind.

Two rules are worth stating because they are asymmetric and the asymmetry is deliberate:

- **The instance ROOT's ``meta`` and ``transform`` never apply.** Where a crate stands in a level
  and what that level calls it are the level's business; writing them into the prefab would move
  every other crate in the game to this crate's spot.
- **A CHILD carrier's ``transform`` and ``Name`` DO apply**, because "the lid sits 2 cm too low"
  is exactly the correction an author reaches for this gesture to make. Only the format's
  addressing fields (``Guid``, ``Parent``, ``Target``, ``Dropped``) are never copied.

Applying changes every instance of the prefab, everywhere, and nothing here can see the documents
this one does not open. The warnings say so; the decision is the author's.

Imports no ``bpy``.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from . import guid as document_guid
from . import overrides, well_known
from .prefab import PrefabComponent, PrefabDocument, PrefabObject

__all__ = ["ApplyError", "ApplyResult", "apply_to_prefab"]

#: Never copied from an override into a prefab object: they address the object rather than
#: describe it, and the resolver rebuilds them on the way out anyway.
_ADDRESSING = (well_known.GUID, well_known.PARENT, well_known.TARGET, well_known.DROPPED)


class ApplyError(Exception):
    """The overrides could not be applied. The message is for the author."""


@dataclass
class ApplyResult:
    """The prefab with the overrides folded in, and the document with them taken off."""

    prefab: PrefabDocument = field(default_factory=PrefabDocument)
    remaining: PrefabDocument = field(default_factory=PrefabDocument)

    #: Component payloads written into the prefab.
    components: int = 0
    #: Prefab children a carrier changed, dropped ones included.
    children: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def applied(self) -> int:
        return self.components + self.children


def apply_to_prefab(
    document: PrefabDocument, instance_guid: str, prefab: PrefabDocument
) -> ApplyResult:
    """Fold instance ``instance_guid``'s overrides into ``prefab``.

    ``prefab`` is the prefab's OWN document as it sits on disk, never a flattened one: what is
    written back has to be the file's objects, and a flattened prefab has inlined away the very
    instances the file declares.
    """
    instance = _instance(document, instance_guid)
    wanted = document_guid.canonical(instance_guid)

    _refuse_unaddressable(document, wanted, prefab)

    result = ApplyResult(prefab=copy.deepcopy(prefab))
    root = result.prefab.root()

    result.components += _apply_components(instance, root, is_root=True, result=result)
    _apply_carriers(document, wanted, result)

    result.remaining = overrides.strip_instance(document, wanted)
    result.warnings.extend(_reach(document, instance, wanted))
    return result


def _instance(document: PrefabDocument, instance_guid: str) -> PrefabObject:
    if document_guid.parse(instance_guid) is None:
        raise ApplyError(f"'{instance_guid}' is not an object identity")
    wanted = document_guid.canonical(instance_guid)

    subject = document.by_guid().get(wanted)
    if subject is None:
        raise ApplyError(f"no object with identity '{wanted}' is in this document")
    if subject.prefab is None:
        raise ApplyError(
            f"'{subject.name or wanted}' does not instance a prefab, so it overrides nothing."
        )
    return subject


def _apply_carriers(document: PrefabDocument, instance_guid: str, result: ApplyResult) -> None:
    """Each carrier's payload onto the prefab child it addresses."""
    members = result.prefab.by_guid()

    for local, carrier in overrides.carriers_of(document, instance_guid).items():
        target = members[local]
        if carrier.dropped:
            _drop(result.prefab, local)
            result.children += 1
            continue

        written = _apply_components(carrier, target, is_root=False, result=result)
        if written:
            result.children += 1


def _refuse_unaddressable(document: PrefabDocument, instance_guid: str, prefab: PrefabDocument) -> None:
    """Refuse when an override addresses a child the prefab's own file does not declare.

    Such a child came out of a prefab nested inside this one; its identity was minted while
    flattening and appears in no file here, so there is nothing to write the override onto.
    ALL or nothing: a half-applied fold takes some overrides off the instance and leaves others,
    and nothing afterwards tells the author which of their edits went where.
    """
    members = prefab.by_guid()
    stranded = [
        local for local in overrides.carriers_of(document, instance_guid) if local not in members
    ]
    if not stranded:
        return

    raise ApplyError(
        f"{len(stranded)} override(s) on this instance address objects that come from a prefab "
        "nested inside this one, so they exist in no file here and cannot be applied:\n"
        + "\n".join(f"  {local}" for local in stranded)
        + "\n\nApply those in the prefab that declares them, or unpack this instance. Nothing "
        "was written."
    )


def _apply_components(
    source: PrefabObject, target: PrefabObject, *, is_root: bool, result: ApplyResult
) -> int:
    """``source``'s override components onto ``target``, returning how many landed.

    On the instance ROOT, ``meta`` and ``transform`` are skipped whole: the level owns where
    this instance stands and what it calls it. On a CHILD carrier both apply -- correcting where
    a prefab's child sits is the reason to reach for this gesture.
    """
    written = 0
    for component in source.components:
        if is_root and component.id in (well_known.META_ID, well_known.TRANSFORM_ID):
            continue

        existing = target.component(component.id)
        if component.removed:
            if component.id in (well_known.META_ID, well_known.TRANSFORM_ID):
                # The resolver lets an instance hide these from ITSELF; applying that would leave
                # the prefab holding an object with no identity, which no reader accepts.
                result.warnings.append(
                    f"an override removes the prefab's own '{component.type or component.id}', "
                    "which cannot be applied -- every object needs one"
                )
                continue
            if existing is None:
                result.warnings.append(
                    f"an override removes component '{component.id}', which the prefab does not "
                    "have -- nothing to apply"
                )
                continue
            target.components.remove(existing)
            written += 1
            continue

        payload = _payload(component)
        if not payload:
            continue
        if existing is None:
            target.components.append(
                PrefabComponent(component.id, component.type, copy.deepcopy(payload))
            )
        else:
            # Shallow, matching `resolve._merge_data`: an override replaces a whole sub-table
            # rather than merging into it, so applying it has to replace the same whole table or
            # the prefab would end up saying something no instance ever showed.
            existing.data.update(copy.deepcopy(payload))
        written += 1
    return written


def _payload(component: PrefabComponent) -> dict:
    """What of this override belongs in a prefab: everything but the addressing fields."""
    if component.id != well_known.META_ID:
        return component.data
    return {key: value for key, value in component.data.items() if key not in _ADDRESSING}


def _drop(prefab: PrefabDocument, local: str) -> None:
    """Remove a prefab child and everything under it. Its descendants would otherwise be
    parented to an object that no longer exists, and the reader refuses that document."""
    doomed = {local}
    growing = True
    while growing:
        growing = False
        for entry in prefab.objects:
            if entry.guid is None or entry.guid in doomed:
                continue
            if entry.parent in doomed:
                doomed.add(entry.guid)
                growing = True

    prefab.objects = [
        entry for entry in prefab.objects
        if entry.guid not in doomed and (entry.target is None or entry.target not in doomed)
    ]


def _reach(document: PrefabDocument, instance: PrefabObject, instance_guid: str) -> list[str]:
    """Who else this is about to change. Only this document can be counted -- every other level
    that places the prefab changes too, and none of them is open."""
    others = sum(
        1 for entry in document.objects
        if entry.prefab is not None
        and entry.guid != instance_guid
        and entry.prefab.path == instance.prefab.path
    )
    warnings = [
        f"applying edits '{instance.prefab.path}' itself: every instance of it changes, in every "
        "document, not only this one"
    ]
    if others:
        warnings.append(f"{others} other instance(s) of it in THIS document will change too")
    return warnings
