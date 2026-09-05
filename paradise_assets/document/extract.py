"""Taking a subtree out of a document and leaving an instance of it behind.

What survives and what does not follows from the resolver (:mod:`resolve`), not from taste:

- The instance keeps the extracted object's OWN guid, and the resolved root's guid IS the
  instance guid, so every reference to that object still names it after the extraction.
- A child's resolved guid becomes ``uuid5(instance guid, prefab-local guid)``, so a reference
  from outside the subtree to a child inside it silently stops naming anything. Those are found
  and warned about; they are not refused, because the fix is an authoring decision.
- The instance is left carrying meta and transform ONLY. Copies of the subtree's components
  would be overrides, and an override shadows the prefab forever -- editing the new prefab would
  appear to do nothing at the one place it was extracted from.

The remaining document is a METHOD rather than a field, and that is the sidecar rule showing
through: the instance needs the new prefab's guid, which only ``paradise assets watch`` mints,
and it does not exist until the prefab document has been written and identified. Making it
impossible to obtain the remaining document without a reference is what stops a caller writing
the level back with a plain object where its instance should be.

Imports no ``bpy``: this is document surgery, and the unit tests are what keep it honest.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field

from . import guid as document_guid
from . import well_known
from .asset_reference import AssetReference
from .new_prefab import IDENTITY_TRANSFORM
from .prefab import PrefabComponent, PrefabDocument, PrefabObject

__all__ = ["ExtractError", "ExtractResult", "extract"]


class ExtractError(Exception):
    """The subtree could not be extracted. The message is for the author."""


@dataclass
class ExtractResult:
    """The prefab an extraction produces, and how to get the document it came out of."""

    prefab: PrefabDocument = field(default_factory=PrefabDocument)
    warnings: list[str] = field(default_factory=list)

    #: The prefab-LOCAL identity given to the prefab's root object. Not an asset identity.
    prefab_root_guid: str = ""

    #: Objects that moved into the prefab, the root included; carriers are counted separately
    #: because they are not objects an author sees in the outliner.
    objects: int = 0
    carriers: int = 0

    _source: PrefabDocument = field(default_factory=PrefabDocument, repr=False)
    _subject: PrefabObject | None = field(default=None, repr=False)
    _travelling: frozenset[int] = field(default_factory=frozenset, repr=False)

    def remaining(self, reference: AssetReference) -> PrefabDocument:
        """The source document with the subtree replaced by one instance of ``reference``.

        Takes the reference rather than holding one, because the prefab's asset identity does
        not exist until the watcher has minted it (:mod:`sidecar`).
        """
        return _remaining_document(self._source, self._subject, self._travelling, reference)


def extract(
    document: PrefabDocument,
    root_guid: str,
    new_root_guid: str | None = None,
) -> ExtractResult:
    """Split ``document`` into the prefab holding ``root_guid``'s subtree and what is left.

    ``new_root_guid`` is only for tests that need the prefab root's identity to be predictable.
    """
    subject = _subject(document, root_guid)
    local = document_guid.canonical(new_root_guid) if new_root_guid else str(uuid.uuid4())

    members = _subtree(document, subject.guid)
    carriers = [
        entry for entry in document.objects
        if entry.target is not None and entry.parent is not None and entry.parent in members
    ]
    travelling = frozenset(
        {id(entry) for entry in carriers} | {id(document.by_guid()[g]) for g in members}
    )

    result = ExtractResult(prefab_root_guid=local)
    result.prefab = _prefab_document(document, subject, members, carriers, local)
    result.objects = len(members)
    result.carriers = len(carriers)
    result._source, result._subject, result._travelling = document, subject, travelling

    # The warnings do not depend on the reference, so they are available before the prefab has
    # been written -- which is when the operator wants to decide whether to go on.
    placeholder = _remaining_document(document, subject, travelling, None)
    result.warnings = _dangling_references(placeholder, members - {subject.guid}, document)
    return result


def _subject(document: PrefabDocument, root_guid: str) -> PrefabObject:
    """The object to extract, or the reason it cannot be."""
    if document_guid.parse(root_guid) is None:
        raise ExtractError(f"'{root_guid}' is not an object identity")
    wanted = document_guid.canonical(root_guid)

    carrier = next(
        (e for e in document.objects if e.target is not None and e.meta is not None
         and e.meta.data.get(well_known.GUID) == wanted),
        None,
    )
    if carrier is not None:
        raise ExtractError(
            "that object is an override on a prefab's child, not an object of its own. "
            "Extract the instance it belongs to, or edit the prefab itself."
        )

    subject = document.by_guid().get(wanted)
    if subject is None:
        raise ExtractError(f"no object with identity '{wanted}' is in this document")
    if subject.parent is None:
        raise ExtractError(
            f"'{subject.name or wanted}' is this document's root, so extracting it would extract "
            "the whole document. Parent what you want under a new object first."
        )
    return subject


def _subtree(document: PrefabDocument, root_guid: str) -> set[str]:
    """``root_guid`` and every descendant of it, by ``meta.Parent``."""
    children: dict[str, list[str]] = {}
    for entry in document.objects:
        if entry.target is not None or entry.guid is None or entry.parent is None:
            continue
        children.setdefault(entry.parent, []).append(entry.guid)

    members = {root_guid}
    frontier = [root_guid]
    while frontier:
        for child in children.get(frontier.pop(), ()):
            if child not in members:
                members.add(child)
                frontier.append(child)
    return members


def _prefab_document(
    document: PrefabDocument,
    subject: PrefabObject,
    members: set[str],
    carriers: list[PrefabObject],
    minted: str,
) -> PrefabDocument:
    """The subtree as a document of its own, in the order it stood in the source: the resolver
    emits a prefab's objects in prefab order, so keeping it keeps the level's outliner order."""
    prefab = PrefabDocument()
    for entry in document.objects:
        if entry.target is not None:
            if entry not in carriers:
                continue
            prefab.objects.append(_reparented(entry, subject.guid, minted))
            continue
        if entry.guid not in members:
            continue
        if entry.guid == subject.guid:
            prefab.objects.append(_prefab_root(entry, minted))
        else:
            prefab.objects.append(_reparented(entry, subject.guid, minted))
    return prefab


def _prefab_root(subject: PrefabObject, minted: str) -> PrefabObject:
    """The extracted object as a prefab root: a new prefab-local identity, no parent, and the
    identity transform -- where it stood in the level belongs to the instance, not to the prefab."""
    root = copy.deepcopy(subject)

    meta = root.meta
    meta.data.pop(well_known.PARENT, None)
    meta.data[well_known.GUID] = minted

    transform = root.component(well_known.TRANSFORM_ID)
    if transform is None:
        root.components.append(
            PrefabComponent(well_known.TRANSFORM_ID, well_known.TRANSFORM_TYPE, dict(IDENTITY_TRANSFORM))
        )
    else:
        transform.data.clear()
        transform.data.update(IDENTITY_TRANSFORM)
    return root


def _reparented(entry: PrefabObject, old_root: str, minted: str) -> PrefabObject:
    """A subtree member verbatim, with a link to the old root re-pointed at the new one."""
    member = copy.deepcopy(entry)
    meta = member.meta
    if meta is not None and meta.data.get(well_known.PARENT) == old_root:
        meta.data[well_known.PARENT] = minted
    return member


def _remaining_document(
    document: PrefabDocument,
    subject: PrefabObject,
    travelling: frozenset[int],
    reference: AssetReference | None,
) -> PrefabDocument:
    """The document with the subtree replaced, in one slot, by an instance of the new prefab."""
    remaining = PrefabDocument()
    for entry in document.objects:
        if id(entry) not in travelling:
            remaining.objects.append(entry)
            continue
        if entry is subject:
            remaining.objects.append(_instance(subject, reference))
    return remaining


def _instance(subject: PrefabObject, reference: AssetReference | None) -> PrefabObject:
    """The instance left behind: identity, name, parent, placement, and nothing else."""
    meta: dict = {well_known.GUID: subject.guid}
    if subject.name is not None:
        meta[well_known.NAME] = subject.name
    if subject.parent is not None:
        meta[well_known.PARENT] = subject.parent

    transform = subject.component(well_known.TRANSFORM_ID)
    placement = copy.deepcopy(transform.data) if transform is not None else dict(IDENTITY_TRANSFORM)

    return PrefabObject(
        prefab=reference,
        components=[
            PrefabComponent(well_known.META_ID, well_known.META_TYPE, meta),
            PrefabComponent(well_known.TRANSFORM_ID, well_known.TRANSFORM_TYPE, placement),
        ],
    )


def _dangling_references(
    remaining: PrefabDocument, children: set[str], source: PrefabDocument
) -> list[str]:
    """Every place the rest of the document still names an extracted CHILD. Those identities are
    about to become ``uuid5(instance, prefab-local)``, so the reference will resolve to nothing."""
    if not children:
        return []

    names = {entry.guid: entry.name for entry in source.objects if entry.guid is not None}
    warnings: list[str] = []
    for entry in remaining.objects:
        for component in entry.components:
            for key, value in component.data.items():
                if component.id == well_known.META_ID and well_known.is_meta_field(key):
                    continue
                for hit in _guid_strings(value, children):
                    warnings.append(
                        f"{entry.name or entry.guid}: {component.type or component.id}.{key} still "
                        f"names '{names.get(hit) or hit}', which moved into the prefab -- its "
                        "identity there is minted per instance, so the reference will not resolve"
                    )
    return warnings


def _guid_strings(value, wanted: set[str]) -> list[str]:
    """Every member of ``wanted`` this payload value spells, at any depth."""
    if isinstance(value, str):
        parsed = document_guid.parse(value)
        if parsed is not None and str(parsed) in wanted:
            return [str(parsed)]
        return []
    if isinstance(value, dict):
        return [hit for member in value.values() for hit in _guid_strings(member, wanted)]
    if isinstance(value, list):
        return [hit for member in value for hit in _guid_strings(member, wanted)]
    return []
