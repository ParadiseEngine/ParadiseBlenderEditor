"""Breaking a prefab instance's link: the objects it displayed become objects this document owns.

The exact inverse of :mod:`extract`, and the interesting difference is what happens to
references. Extraction MINTS new identities for the children that move into the prefab, so a
reference from outside to one of them stops naming anything and has to be warned about. Unpacking
mints nothing: the resolver already gave every displayed child the identity
``uuid5(instance, prefab-local)``, and unpacking writes exactly that identity into the document.
So every reference that resolved before resolves after, including references to the instance
itself, whose guid the resolved root already carries.

Prefabs nested inside the prefab are flattened too, because resolution flattens them: what the
author sees is what they get. An instance of a prefab that itself instances something is one
gesture away from being all plain objects, and there is no half-way state to explain.

Imports no ``bpy``.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from . import guid as document_guid
from . import overrides, resolve, well_known
from .prefab import PrefabDocument, PrefabObject

__all__ = ["UnpackError", "UnpackResult", "unpack"]


class UnpackError(Exception):
    """The instance could not be unpacked. The message is for the author."""


@dataclass
class UnpackResult:
    """The document with one instance replaced by the objects it was showing."""

    document: PrefabDocument = field(default_factory=PrefabDocument)
    #: Objects the instance became, its root included.
    objects: int = 0
    #: Override carriers consumed -- they are the instance's data and go with it.
    carriers: int = 0
    warnings: list[str] = field(default_factory=list)


def unpack(document: PrefabDocument, instance_guid: str, prefabs) -> UnpackResult:
    """Replace the instance ``instance_guid`` with the plain objects it resolves to."""
    instance = _instance(document, instance_guid)
    wanted = document_guid.canonical(instance_guid)

    carriers = overrides.carriers_of(document, wanted)
    expanded = _expanded(instance, carriers.values(), prefabs)

    if not expanded.document.objects:
        raise UnpackError(
            f"'{instance.name or wanted}' instances '{instance.prefab.path}', which could not be "
            "resolved, so there is nothing to unpack it into. Fix the prefab first "
            "(`paradise assets verify`); unpacking now would delete the objects it stands for."
        )

    result = UnpackResult(
        objects=len(expanded.document.objects),
        carriers=len(carriers),
        warnings=list(expanded.errors),
    )

    # In the instance's own slot, in resolver order: an author's outliner ordering survives, and
    # so does the entity walk order a bake assigns handles in.
    # Deep-copied: `resolve._merge` appends the PREFAB's own component objects by reference where
    # an instance overrides nothing, and the spliced objects are about to be written to and
    # edited. Without this, a save would mutate the prefab document sitting in the resolver's
    # cache -- a different asset, in memory only, and nothing would say so.
    consumed = {id(entry) for entry in carriers.values()}
    for entry in document.objects:
        if entry is instance:
            result.document.objects.extend(copy.deepcopy(expanded.document.objects))
        elif id(entry) not in consumed:
            result.document.objects.append(entry)

    return result


def _instance(document: PrefabDocument, instance_guid: str) -> PrefabObject:
    """The instance to unpack, or the reason it cannot be."""
    if document_guid.parse(instance_guid) is None:
        raise UnpackError(f"'{instance_guid}' is not an object identity")
    wanted = document_guid.canonical(instance_guid)

    carrier = next(
        (e for e in document.objects
         if e.target is not None and e.meta is not None
         and e.meta.data.get(well_known.GUID) == wanted),
        None,
    )
    if carrier is not None:
        raise UnpackError(
            "that object is an override on a prefab's child, not an instance. "
            "Unpack the instance it belongs to."
        )

    subject = document.by_guid().get(wanted)
    if subject is None:
        # A resolved child's identity is minted and appears in no file, so this is also what
        # "you selected a prefab's child" looks like from here; the operator, which can see the
        # instance the object belongs to, says so in those words.
        raise UnpackError(f"no object with identity '{wanted}' is in this document")

    if subject.prefab is None:
        raise UnpackError(
            f"'{subject.name or wanted}' does not instance a prefab, so there is no link to break."
        )
    return subject


def _expanded(instance: PrefabObject, carriers, prefabs) -> resolve.ResolveResult:
    """The instance resolved on its own -- root plus children, overrides folded in, no prefab
    reference left. A document of just this instance and its carriers is enough: ``resolve``
    neither validates roots nor follows parents out of the document it is given, so what comes
    back is exactly the objects the loader was already showing for this instance."""
    return resolve.resolve(PrefabDocument([instance, *carriers]), prefabs)
