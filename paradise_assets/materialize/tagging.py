"""Everything the ``.blend`` records about a document, in one place.

The load and the save both have to say what an object is -- its resolved components, what the
prefab alone said, how a carrier addresses it, which components its own file entry authors. They
used to say it in two places, and they disagreed: ``save._refresh_snapshots`` rewrote an
instance's payload from the LEVEL entry (meta, transform, overrides) over the RESOLVED payload
the load had put there, so saving a level collapsed every instance's Components panel to two rows
until the next reload.

One function tags, one function resolves, and both callers use both. A disagreement between the
load's view and the save's is then not a bug that can be written.

Nothing here is written back to a document: the payloads are display data, and ID properties
normalize types (``int`` -> ``float``), which is a bug in data promised verbatim.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..document import overrides, project, resolve
from ..document.prefab import PrefabDocument, PrefabObject
from ..document.prefab import loads as parse_document
from . import store

__all__ = ["Resolution", "resolve_document", "tag"]


@dataclass
class Resolution:
    """A document resolved for display, with the two maps authoring needs beside it."""

    document: PrefabDocument = field(default_factory=PrefabDocument)
    errors: list[str] = field(default_factory=list)
    expanded: int = 0

    #: Every file the resolution READ, so a cache can key on what was touched.
    sources: set[str] = field(default_factory=set)

    #: guid -> what the PREFAB alone says about it (:func:`overrides.baseline`).
    base: dict[str, PrefabObject] = field(default_factory=dict)
    #: resolved child guid -> how a carrier addresses it.
    locals: dict[str, overrides.LocalRef] = field(default_factory=dict)

    #: instance guid -> the reference its entry carries. Read before the expansion, which
    #: replaces the entry with the prefab's resolved root and so consumes it.
    instanced: dict = field(default_factory=dict)
    #: guid -> the component ids that object's OWN file entry carries.
    authored: dict[str, list[str]] = field(default_factory=dict)

    def children_of(self, instance_guid: str) -> set[str]:
        """The prefab-local guids resolved under one instance."""
        return {
            ref.local for ref in self.locals.values() if ref.instance == instance_guid
        }


def resolve_document(document: PrefabDocument, layout: project.ProjectLayout, warn=None) -> Resolution:
    """Resolve ``document`` for display and work out what each object overrides.

    One memoized prefab reader serves the display expansion, the baseline and the local-guid
    minting: three passes over the same handful of files otherwise, and a level places one prop
    a hundred times.
    """
    warn = warn or (lambda _message: None)
    result = Resolution()
    cache: dict[str, PrefabDocument | None] = {}

    def prefabs(reference):
        if reference.path not in cache:
            cache[reference.path] = _read(layout, reference, result, warn)
        return cache[reference.path]

    expansion = resolve.resolve(document, prefabs)
    result.document = expansion.document
    result.errors = list(expansion.errors)
    result.expanded = expansion.expanded

    result.base = overrides.baseline(document, prefabs)
    result.locals = overrides.locals_of(document, prefabs)
    result.instanced = {
        entry.guid: entry.prefab
        for entry in document.objects
        if entry.guid is not None and entry.prefab is not None
    }
    result.authored = {
        entry.guid: [component.id for component in entry.components]
        for entry in document.objects
        if entry.guid is not None and entry.target is None
    }
    return result


def _read(layout: project.ProjectLayout, reference, result: Resolution, warn):
    """A referenced prefab, reporting rather than raising."""
    path = layout.resolve(reference.path)
    result.sources.add(os.path.normcase(os.path.abspath(path)))
    try:
        with open(path, encoding="utf-8") as handle:
            return parse_document(handle.read(), path)
    except OSError:
        warn(f"prefab '{reference.path}' could not be read")
        return None
    except Exception as error:   # PrefabDocumentError, reported not raised
        warn(str(error))
        return None


def tag(obj, entry: PrefabObject, resolution: Resolution) -> None:
    """Record on ``obj`` everything the ``.blend`` knows about the document object ``entry``.

    Called for every object the load creates and again for every object the save has just
    written, from the same resolution, so the two views cannot drift.
    """
    guid = entry.guid
    store.tag_object(obj, guid, payload(entry))

    base = resolution.base.get(guid)
    store.tag_base(obj, payload(base) if base is not None else [])

    authored = resolution.authored.get(guid)
    if authored is not None:
        store.tag_authored(obj, authored)
    else:
        # Resolved out of a prefab: its own entry authors nothing, so nothing it shows is
        # written as an object of its own -- only ever as an override on the instance.
        store.mark_derived(obj)
        store.tag_authored(obj, [])

    local = resolution.locals.get(guid)
    if local is not None:
        store.tag_local(obj, local.instance, local.local, local.own)

    reference = resolution.instanced.get(guid)
    if reference is not None:
        store.tag_prefab(obj, reference.guid, reference.path)
        store.tag_children(obj, resolution.children_of(guid))

    store.mark(obj, entry.name, suffix=mark_for(obj, entry, resolution))


def mark_for(obj, entry: PrefabObject, resolution: Resolution) -> str:
    """The Outliner suffix for this object.

    It tracks the DOCUMENT, not the edit overlay: it is written from the load and from the save,
    both of which are operators, and never from a draw -- Blender forbids writing an ID property
    there, and renaming at redraw rate would be worse. So a pending, unsaved override shows in
    the Document Tree panel (which reads live state) and reaches the name on the next save.
    """
    if resolution.instanced.get(entry.guid) is not None:
        base = store.MARK_INSTANCE
    elif entry.guid in resolution.locals:
        base = store.MARK_DERIVED
    else:
        return ""
    return base + (store.MARK_OVERRIDDEN if overridden(obj) else "")


def overridden(obj) -> bool:
    """Whether what this object shows differs from what its prefab says."""
    base = {str(c.get("id", "")).lower(): c.get("data") for c in store.base_json(obj)}
    if not base:
        return False
    shown = {str(c.get("id", "")).lower(): c.get("data") for c in store.component_json(obj)}
    if set(base) != set(shown):
        return True
    return any(
        overrides.differs(
            base[key] if isinstance(base[key], dict) else {},
            shown[key] if isinstance(shown[key], dict) else {},
        )
        for key in base
    )


def payload(entry: PrefabObject) -> list:
    """An object's components in the shape the panel and the JSON store want."""
    return [
        {"id": component.id, "type": component.type, "data": component.data}
        for component in entry.components
    ]
