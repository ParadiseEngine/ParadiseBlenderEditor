"""The ``meta`` and ``transform`` components, resolved once per export over the set of objects
that will be written: since v6 the document carries local TRS plus a parent guid, and neither
identity nor "which exported object is this hanging from" is answerable one object at a time.

:func:`identity` is THE identity function of this host. ``meta.Guid``, a ``HostId`` self
reference, a ``HostEntity`` reference and a ``HostParent`` reference are all the same value for
the same object, which the engine states outright (``HostKinds.cs``: a reference "is the same
GUID its meta carries"); a second minting anywhere is a reference no object answers to (#27).
"""

from __future__ import annotations

import uuid

import bpy

from ..contract import well_known
from ..contract.schema import AuthoredComponentData, EntityComponentsData
from .transform import decompose_contract

__all__ = ["ParentIdentity", "Placement", "identity"]

#: Fixed forever: changing it re-mints every identity in every scene at once.
_NAMESPACE = uuid.UUID("6f0f2a1c-8b3d-4e57-9a24-0d5c7e1b3f88")


def identity(name: str) -> str:
    """An object's GUID, derived from its name (uuid5; ``hash()`` is salted per run).

    Derived, not stored, because a stored id makes exporting MUTATE the .blend, which is
    LFS-locked in the consuming repos; the cost is that a rename re-mints the identity -- the
    same exposure v5 had, where the name WAS the handle. Blender guarantees names are unique
    within a file, so the derivation is injective per scene.
    """
    return str(uuid.uuid5(_NAMESPACE, name))


class ParentIdentity:
    """A ``HostParent`` value baked before the exported set is known. The bake runs per object,
    but the parent is the nearest EXPORTED ancestor, which only :class:`Placement` can name;
    it replaces every marker in the payloads it places."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "ParentIdentity()"


class Placement:
    """Identity and local placement for one export, over the objects that will be written."""

    def __init__(self, exported: set[str]) -> None:
        #: Object NAMES, not objects. Blender guarantees names are unique within a file, and a
        #: name survives the pointer invalidation a purge or a join can cause.
        self._exported = exported

    def parent_of(self, obj: bpy.types.Object) -> bpy.types.Object | None:
        """The nearest EXPORTED ancestor, or ``None``. An empty that authors nothing is not in
        the document, and naming it would make the child a root standing in the wrong place."""
        ancestor = obj.parent
        while ancestor is not None:
            if ancestor.name in self._exported:
                return ancestor
            ancestor = ancestor.parent
        return None

    def components(self, obj: bpy.types.Object, components: EntityComponentsData) -> None:
        """Prepend ``meta`` and ``transform`` so a document reads identity-first, and settle
        every :class:`ParentIdentity` the bakes left behind."""
        parent = self.parent_of(obj)
        parent_guid = identity(parent.name) if parent is not None else None

        for component in components.components:
            component.data = _settle_parent(component.data, parent_guid or "")

        meta = AuthoredComponentData(
            id=well_known.META_ID,
            type=well_known.META_TYPE,
            data=well_known.meta_payload(
                guid=identity(obj.name),
                name=obj.name,
                parent=parent_guid,
            ),
        )

        position, rotation, scale, _ = decompose_contract(self.local_matrix(obj, parent))
        transform = AuthoredComponentData(
            id=well_known.TRANSFORM_ID,
            type=well_known.TRANSFORM_TYPE,
            data=well_known.transform_payload(position, rotation, scale),
        )

        components.components[:0] = [meta, transform]

    @staticmethod
    def local_matrix(obj: bpy.types.Object, parent: bpy.types.Object | None):
        """Placement relative to *parent* in Blender's basis, from the two world matrices:
        ``matrix_local`` is relative to the IMMEDIATE parent and folds in
        ``matrix_parent_inverse``. Blender basis because ``decompose_contract`` must convert the
        matrix and decompose the RESULT, the only order that gets scale right ((1,2,3) -> (1,3,2))."""
        if parent is None:
            return obj.matrix_world.copy()
        return parent.matrix_world.inverted_safe() @ obj.matrix_world


def _settle_parent(value, parent_guid: str):
    """*value* with every :class:`ParentIdentity` replaced; "" for a root, as ``HostParent``
    defines it (``Guid.Empty``)."""
    if isinstance(value, ParentIdentity):
        return parent_guid
    if isinstance(value, dict):
        return {key: _settle_parent(member, parent_guid) for key, member in value.items()}
    if isinstance(value, list):
        return [_settle_parent(member, parent_guid) for member in value]
    return value
