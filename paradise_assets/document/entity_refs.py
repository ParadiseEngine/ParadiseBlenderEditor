"""Entity references: payload fields the game declares ``authoredBy: entity`` (``HostEntity``).

Such a field stores the GUID of another object -- "the same identity its ``meta`` carries" -- and
the runtime looks it up once the whole scene is built. The resolver remaps only ``meta.Guid`` and
``meta.Parent`` (``resolve._rewrite_meta``); a GUID inside a payload is carried verbatim, so what
the field names has to be a guid the RESOLVED document actually contains. Two kinds qualify:

- **a document entry** -- an ordinary object, or an instance ROOT, whose resolved guid IS the
  instance guid by design;
- **a resolved child of an instance** -- including content of a prefab nested inside that prefab
  -- whose identity is ``uuid5(instance guid, flattened prefab-local guid)``
  (``resolve.mint_child_guid``). It exists in no file, but it is deterministic, it is what C#
  ``PrefabResolver.MintChildGuid`` produces, and it stays the same across reloads, saves, renames,
  prefab edits that keep the member, and duplication of EITHER side: a duplicated instance mints
  a new instance guid and so a different child, which the reference does not follow -- it keeps
  naming the original's (#68, #71).

What is NOT stable is a reference whose target is gone: the instance deleted or re-placed (a new
instance guid), or the member removed from the prefab. That used to be silent -- the payload kept
a GUID that named nothing, and the game found out at load. :func:`check` makes it an authored
error: the save refuses a document whose OWN entity values name nothing it resolves to, and warns
about values a placed prefab contributes, which the level's author cannot fix from here.

A prefab that names one of its own members by its prefab-LOCAL guid resolves inside that prefab
and dangles in every instance of it (the member answers to its minted guid there). Rewriting
payload references on expansion is a normative, cross-language format change (C#
``PrefabResolver`` first -- docs/document-contracts.md, "When the document format changes"), so
it is reported here rather than silently papered over in one of the two mirrors.

Imports no ``bpy``: the unit tests are what keep it honest.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from . import guid as document_guid
from . import resolve, well_known
from .overrides import LocalRef
from .prefab import PrefabDocument

__all__ = [
    "ENTITY_KIND",
    "Problem",
    "Report",
    "Target",
    "check",
    "entity_values",
    "is_entity_field",
    "is_unset",
    "targets",
]

#: ``AuthoredBySources.Entity``: the schema's ``authoredBy`` for a ``HostEntity`` member.
ENTITY_KIND = "entity"


def is_entity_field(field) -> bool:
    """Whether this schema member stores another object's identity (one value, not a list)."""
    return getattr(field, "authored_by", None) == ENTITY_KIND and getattr(field, "type", "") != "array"


def is_unset(value) -> bool:
    """An entity value that names nothing on purpose: absent, ``""`` or the all-zero guid.

    Whether a component may be left unset is the GAME's rule (``CameraTarget`` refuses
    ``Guid.Empty`` at load); the editor's rule is only that a value it DOES spell names
    something.
    """
    if value is None or value == "":
        return True
    parsed = document_guid.parse(value)
    return parsed is not None and parsed.int == 0


@dataclass(frozen=True)
class Target:
    """One object an entity field may name: its resolved guid and how to show it.

    ``instance``/``local`` are set for a resolved child, the same ``(instance, local)`` pair an
    override carrier spells (:class:`~.overrides.LocalRef`), so the panel can say where it lives.
    """

    guid: str
    name: str
    label: str
    instance: str | None = None
    local: str | None = None


@dataclass(frozen=True)
class Problem:
    """An entity value that names nothing this document resolves to."""

    owner: str
    owner_name: str
    component_id: str
    component_name: str
    path: str
    value: object
    reason: str
    inherited: bool = False

    def message(self) -> str:
        where = f"{self.owner_name or self.owner} > {self.component_name}.{self.path.replace('/', '.')}"
        if self.inherited:
            return f"{where} {self.reason} (it comes from the placed prefab, so fix it there)"
        return f"{where} {self.reason}"


@dataclass
class Report:
    """What :func:`check` found: errors the save refuses, warnings it only reports."""

    errors: list[Problem] = field(default_factory=list)
    warnings: list[Problem] = field(default_factory=list)

    def refusal(self) -> str:
        lines = [problem.message() for problem in self.errors]
        return (
            "An entity reference names no object in this document:\n  "
            + "\n  ".join(lines)
            + "\n\nPick the target again in the Components panel (or clear the field). A reference"
            " keeps naming the object it was picked from: deleting or re-placing that object, or"
            " removing it from its prefab, leaves nothing for it to name."
        )


def entity_values(fields, data, prefix: str = "") -> Iterator[tuple[str, object]]:
    """Every entity-typed value in a payload, as ``(slash path, value)``, in declaration order.

    Walks nested records and lists of them, and lists OF entity references, the way the panel's
    plan does. A field absent from the payload yields nothing -- "unset" is not a dangling name.
    """
    if not isinstance(data, dict):
        return
    for member in fields or ():
        if member.name not in data:
            continue
        value = data[member.name]
        path = f"{prefix}/{member.name}" if prefix else member.name
        if member.type == "array":
            items = member.items
            if items is None or not isinstance(value, list):
                continue
            for index, row in enumerate(value):
                row_path = f"{path}/{index}"
                if items.authored_by == ENTITY_KIND:
                    yield row_path, row
                elif items.fields:
                    yield from entity_values(items.fields, row, row_path)
            continue
        if member.authored_by == ENTITY_KIND:
            yield path, value
        elif member.fields:
            yield from entity_values(member.fields, value, path)


def targets(resolved: PrefabDocument, locals_map: dict[str, LocalRef] | None = None) -> dict[str, Target]:
    """Every object an entity field may name, by LOWERCASE resolved guid.

    ``resolved`` is the expansion (``resolve.resolve(...).document``); ``locals_map`` is
    ``overrides.locals_of`` for the same document, so a resolved child can be shown as "Door in
    Shop" rather than as a guid nobody can read. A label is the object's name, then the chain of
    the objects it hangs under, innermost first, so two "Door"s in two shops are told apart.
    """
    locals_map = {key.lower(): value for key, value in (locals_map or {}).items()}
    by_guid = {entry.guid.lower(): entry for entry in resolved.objects if entry.guid}
    root = resolved.single_root()
    root_guid = (root.guid or "").lower() if root is not None else ""

    found: dict[str, Target] = {}
    for key, entry in by_guid.items():
        name = entry.name or entry.guid
        chain: list[str] = []
        seen = {key}
        parent = (entry.parent or "").lower()
        while parent and parent not in seen and parent in by_guid and parent != root_guid:
            seen.add(parent)
            ancestor = by_guid[parent]
            chain.append(ancestor.name or ancestor.guid)
            parent = (ancestor.parent or "").lower()
        label = name if not chain else f"{name}  ({' / '.join(chain)})"
        ref = locals_map.get(key)
        found[key] = Target(
            guid=entry.guid,
            name=name,
            label=label,
            instance=ref.instance if ref is not None else None,
            local=ref.local if ref is not None else None,
        )
    return found


def check(document: PrefabDocument, resolved: PrefabDocument, vocabulary,
          locals_map: dict[str, LocalRef] | None = None) -> Report:
    """Every entity value that names nothing ``resolved`` contains.

    ERRORS are the values ``document`` itself spells -- on an ordinary object, on an instance's
    own entry, or on an override carrier -- because this document's author put them there and can
    pick again. WARNINGS are values that only the resolved view carries, folded in from a placed
    prefab: real, and worth saying, but not this document's to fix.

    ``vocabulary`` is the game's schema (``component_schema.Vocabulary``); without it nothing is
    known to be an entity field and nothing is reported. Only a DUMPED component can declare one:
    an inferred schema cannot tell a GUID-valued string from any other.
    """
    report = Report()
    known = targets(resolved, locals_map)
    names = {entry.guid.lower(): entry.name or "" for entry in resolved.objects if entry.guid}
    own: set[tuple[str, str, str]] = set()

    for entry in document.objects:
        if entry.target is not None:
            if entry.parent is None or not document_guid.is_text(entry.parent) \
                    or not document_guid.is_text(entry.target):
                continue
            owner = resolve.mint_child_guid(entry.parent, entry.target)
        elif entry.guid is not None:
            owner = entry.guid
        else:
            continue
        for component in entry.components:
            if component.removed or component.id in (well_known.META_ID, well_known.TRANSFORM_ID):
                continue
            schema = vocabulary.get(component.id) if vocabulary is not None else None
            if schema is None:
                continue
            for path, value in entity_values(schema.fields, component.data):
                own.add((owner.lower(), component.id.lower(), path))
                reason = _dangling(value, known)
                if reason is not None:
                    report.errors.append(Problem(
                        owner, names.get(owner.lower(), entry.name or ""), component.id,
                        schema.display_name, path, value, reason))

    for entry in resolved.objects:
        if entry.guid is None:
            continue
        for component in entry.components:
            schema = vocabulary.get(component.id) if vocabulary is not None else None
            if schema is None:
                continue
            for path, value in entity_values(schema.fields, component.data):
                if (entry.guid.lower(), component.id.lower(), path) in own:
                    continue
                reason = _dangling(value, known)
                if reason is not None:
                    report.warnings.append(Problem(
                        entry.guid, entry.name or "", component.id, schema.display_name, path,
                        value, reason, inherited=True))
    return report


def _dangling(value, known: dict[str, Target]) -> str | None:
    """Why ``value`` names nothing, or ``None`` when it is unset or names a target."""
    if is_unset(value):
        return None
    parsed = document_guid.parse(value)
    if parsed is None:
        return f"holds {value!r}, which is not an object identity (a GUID)"
    if str(parsed) not in known:
        return f"names {parsed!s}, which is no object in this document"
    return None
