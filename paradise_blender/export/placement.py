"""Who an object IS and where it stands relative to its parent -- the two well-known components,
built for a whole export at once.

**This module exists because v6 put the hierarchy back.** v5 wrote each object's world matrix and
dropped the parent link, on the grounds that the engine's bake flattened the tree anyway. Since v6
the bake is a passthrough: the document carries local TRS plus a parent GUID and the LOADER
composes the chain. So the exporter has to answer two questions it never had to before -- what is
this object's identity, and which exported object is it hanging from -- and neither is answerable
one object at a time.

Hence a resolver built once per export, over the set of objects that will actually be written.
"""

from __future__ import annotations

import uuid

import bpy

from ..contract import well_known
from ..contract.schema import AuthoredComponentData, EntityComponentsData
from .transform import decompose_contract

__all__ = ["Placement"]

#: The namespace object identities are derived under.
#:
#: A FIXED, arbitrary UUID: what matters is only that it never changes, because changing it
#: re-mints every identity in every scene at once.
_NAMESPACE = uuid.UUID("6f0f2a1c-8b3d-4e57-9a24-0d5c7e1b3f88")


class Placement:
    """Identity and local placement for one export.

    Constructed with the objects that will be written, because both answers depend on that set: an
    identity has to be stable, and a parent has to be an object the document actually contains.
    """

    def __init__(self, exported: set[str]) -> None:
        #: Object NAMES, not objects. Blender guarantees names are unique within a file, and a
        #: name survives the pointer invalidation a purge or a join can cause.
        self._exported = exported

    def identity(self, name: str) -> str:
        """This object's durable GUID, derived from its name.

        **Derived rather than stored, and the trade is worth stating.** The alternative is minting
        a GUID on first export and writing it into an ID property, which would survive a rename --
        the thing this does not. It was rejected because it makes EXPORTING MUTATE THE .BLEND: the
        file is Git LFS-locked in the game repos that consume this, so a read-only checkout could
        no longer export at all, and every export would dirty a binary that cannot be merged.

        The cost is real: renaming an object re-mints its identity, so anything holding the old
        GUID is orphaned by a rename. That is the same exposure v5 had, where a NAME was the only
        handle an object had, and no worse. When identity needs to survive renames it needs a
        stored id and a decision about the lock -- not a different derivation.

        ``uuid5`` rather than a hash of the name: it is the standard's own name-based form, stable
        across processes and Python versions (``hash()`` is salted per run, so it is not), and it
        produces a well-formed UUID rather than something that merely looks like one.
        """
        return str(uuid.uuid5(_NAMESPACE, name))

    def parent_of(self, obj: bpy.types.Object) -> bpy.types.Object | None:
        """The nearest ancestor this export will actually write, or ``None`` for a root.

        **Nearest EXPORTED, not immediate.** An author is free to park objects under an empty that
        authors nothing, and such an empty is not in the document -- so naming it as a parent would
        produce a link pointing at an object that does not exist, and a loader would treat the
        child as a root standing in the wrong place. Skipping to the next exported ancestor keeps
        the composed world matrix correct however the .blend is organised.

        This is the rule the pre-v5 exporter had, for the same reason, and it is the one part of
        the old parent handling worth bringing back verbatim.
        """
        ancestor = obj.parent
        while ancestor is not None:
            if ancestor.name in self._exported:
                return ancestor
            ancestor = ancestor.parent
        return None

    def components(self, obj: bpy.types.Object, components: EntityComponentsData) -> None:
        """Prepend this object's ``meta`` and ``transform`` to its component list.

        PREPENDED rather than appended so a document reads identity-first, which is what makes one
        scannable by eye. Nothing depends on the order: the contract's list has no fixed positions
        and every reader looks components up by id.
        """
        parent = self.parent_of(obj)
        meta = AuthoredComponentData(
            id=well_known.META_ID,
            type=well_known.META_TYPE,
            data=well_known.meta_payload(
                guid=self.identity(obj.name),
                name=obj.name,
                parent=self.identity(parent.name) if parent is not None else None,
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
        """This object's placement relative to *parent*, in BLENDER's basis.

        Computed from the two world matrices rather than read from ``matrix_local``, because that
        is relative to the IMMEDIATE parent and also folds in ``matrix_parent_inverse`` -- neither
        of which suits a skipped-ancestor chain. Deriving it keeps one rule: the local matrix is
        whatever takes you from the parent this document NAMES to this object, and for a root that
        is the world matrix itself.

        Returned in Blender's basis on purpose. :func:`.transform.decompose_contract` converts the
        assembled matrix and decomposes the RESULT, which is the only order that gets scale right
        -- a Blender scale of (1, 2, 3) is a contract scale of (1, 3, 2), because the basis change
        permutes the axes. Converting first and dividing second would not.
        """
        if parent is None:
            return obj.matrix_world.copy()
        return parent.matrix_world.inverted_safe() @ obj.matrix_world
