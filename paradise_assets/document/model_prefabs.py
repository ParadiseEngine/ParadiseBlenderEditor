"""Keeping one generated prefab per model, so a dropped ``.glb`` is placeable straight away.

The Godot host's ``ModelPrefabGenerator`` is the parity reference: ``prefabs/models/<stem>``,
and an existing prefab is never overwritten.

**What makes a prefab generated is ``meta.GeneratedFrom = "<model guid>"`` on its root**, not its
path and not its file name. Three things follow, and each answers a way this mirror could eat
someone's work:

- The mapping is model IDENTITY to prefab, so renaming or moving a model is an update here, never
  a delete followed by a create -- which would drop every reference a level had.
- A prefab without the field is hand-authored: never rewritten, never deleted, even when it sits
  exactly where a generated one would go. That case is reported and skipped.
- Deleting is the only destructive step, so it is fenced twice: the model must have been gone for
  several consecutive polls (a Finder move arrives as delete-then-add, and the identity is
  relinked in between), and no document may still reference the prefab.

The marker lives in ``meta`` rather than in the sidecar because the sidecar belongs to
``paradise assets watch`` -- the addon only reads those (:mod:`sidecar`) -- and because ``meta``
is open: an unknown field is accepted by the reader and carried through by the resolver.

A renamed model does NOT rename its prefab. The reference's guid is the identity and the path is
a hint, so the mirror refreshes the path inside its own document and leaves the file where the
author's levels already point at it.

Imports no ``bpy``: the diff is pure, and the host half is a timer that calls it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import asset_reference, assets, atomic, gltf, new_prefab, sidecar, well_known
from . import prefab as prefab_document
from . import schema as document_schema
from .asset_reference import AssetReference
from .prefab import PrefabComponent, PrefabDocumentError
from .project import ProjectLayout

__all__ = [
    "GENERATED_FROM",
    "GRACE_POLLS",
    "GRACE_SECONDS",
    "MODELS_DIR",
    "MODEL_EXTENSIONS",
    "Absence",
    "Create",
    "Delete",
    "GeneratedPrefab",
    "MeshChoice",
    "MirrorPlan",
    "Model",
    "Move",
    "Skip",
    "apply",
    "list_models",
    "plan",
    "read_generated",
    "referenced_prefabs",
]

#: The ``meta`` field that marks a prefab as this mirror's to maintain, holding the model's guid.
GENERATED_FROM = "GeneratedFrom"

#: Where a generated prefab is created, assets-relative. One an author has MOVED is maintained
#: where it now lives -- the marker, not the path, is what makes it ours.
MODELS_DIR = "prefabs/models"

MODEL_EXTENSIONS = (".glb",)

#: A deletion waits for this many consecutive polls AND this many seconds, because a Finder move
#: reaches the watcher as a delete and an add, and the identity is relinked in between.
GRACE_POLLS = 2
GRACE_SECONDS = 10.0


@dataclass(frozen=True)
class Model:
    """A model, the one fact that decides which component stands for it, and the document a
    prefab references for it.

    The prefab never references the ``.glb``: a GLB ships nothing, and the build refuses a
    reference to one. What it references is the ``.mesh`` document ``paradise assets watch``
    mints beside the model, recorded in the model's sidecar under ``[glb] mesh``. ``mesh`` is
    ``None`` until the watcher has minted it, and a model without one is skipped, not guessed.
    """

    reference: AssetReference
    skinned: bool
    mesh: AssetReference | None = None

    @property
    def guid(self) -> str:
        return self.reference.guid

    @property
    def path(self) -> str:
        return self.reference.path

    @property
    def stem(self) -> str:
        return os.path.splitext(os.path.basename(self.path))[0]


@dataclass(frozen=True)
class MeshChoice:
    """Which component a generated prefab authors its mesh into, per kind of model.

    Never one component for both: a rigged model authored as static is a prefab that loads,
    shows the mesh, and is the wrong kind of thing in the game.
    """

    static: document_schema.MeshComponent | None = None
    skinned: document_schema.MeshComponent | None = None

    def for_model(self, model: Model) -> document_schema.MeshComponent | None:
        return self.skinned if model.skinned else self.static


@dataclass(frozen=True)
class GeneratedPrefab:
    """A prefab this mirror owns: where it is, what it identifies, and what it stands for."""

    path: str
    relative: str
    guid: str
    model_guid: str

    #: Where its mesh reference points today, and which component holds it. ``None`` when the
    #: document carries no mesh reference at all, which an author's edit can produce.
    mesh_path: str | None = None
    mesh_component_id: str | None = None
    mesh_field: str | None = None

    @property
    def stem(self) -> str:
        return os.path.splitext(os.path.basename(self.relative))[0]


@dataclass(frozen=True)
class Absence:
    """How long a generated prefab's model has been missing."""

    polls: int
    since: float


@dataclass(frozen=True)
class Create:
    """No prefab stands for this model yet."""

    model: Model
    relative: str
    mesh: document_schema.MeshComponent


@dataclass(frozen=True)
class Move:
    """The model this prefab stands for lives somewhere else now."""

    prefab: GeneratedPrefab
    model: Model


@dataclass(frozen=True)
class Delete:
    """The model is gone, and has been for long enough to believe it."""

    prefab: GeneratedPrefab
    model_guid: str


@dataclass(frozen=True)
class Skip:
    """Something the mirror deliberately did not touch. Reported, never done silently."""

    reason: str


@dataclass
class MirrorPlan:
    """What one reconcile pass should do, and what it must remember for the next one."""

    actions: list = field(default_factory=list)
    absences: dict[str, Absence] = field(default_factory=dict)


def plan(
    layout: ProjectLayout,
    components: MeshChoice,
    models: list[Model],
    generated: list[GeneratedPrefab],
    referenced: frozenset[str] = frozenset(),
    absences: dict[str, Absence] | None = None,
    now: float = 0.0,
    grace_polls: int = GRACE_POLLS,
    grace_seconds: float = GRACE_SECONDS,
) -> MirrorPlan:
    """Diff the models against the prefabs standing for them.

    Pure: everything it needs is an argument, the clock included, so the grace period before a
    delete is testable without waiting for it.
    """
    result = MirrorPlan()
    by_model: dict[str, GeneratedPrefab] = {}
    for prefab in generated:
        if prefab.model_guid in by_model:
            result.actions.append(Skip(
                f"{prefab.relative} and {by_model[prefab.model_guid].relative} both stand for the "
                f"same model; delete one, or clear its {GENERATED_FROM} to keep it by hand"
            ))
            continue
        by_model[prefab.model_guid] = prefab

    live = {model.guid for model in models}
    claimed: set[str] = set()

    for model in models:
        prefab = by_model.get(model.guid)
        if prefab is None:
            result.actions.append(_creation(layout, components, model, claimed))
            continue
        if model.mesh is not None and prefab.mesh_path != model.mesh.path:
            result.actions.append(Move(prefab, model))

    for model_guid, prefab in by_model.items():
        if model_guid in live:
            continue
        if prefab.guid in referenced:
            result.actions.append(Skip(
                f"{prefab.relative} stands for a model that is gone, but a document still "
                "instantiates it, so it is kept -- delete the instance first"
            ))
            continue

        seen = (absences or {}).get(model_guid)
        absence = Absence(1, now) if seen is None else Absence(seen.polls + 1, seen.since)
        if absence.polls >= grace_polls and now - absence.since >= grace_seconds:
            result.actions.append(Delete(prefab, model_guid))
        else:
            result.absences[model_guid] = absence

    return result


def _creation(layout: ProjectLayout, components: MeshChoice, model: Model, claimed: set[str]):
    if model.mesh is None:
        return Skip(
            f"{model.path} has no mesh document yet, so no prefab was generated for it -- the "
            "asset watcher mints one beside the model; it is picked up on the next pass"
        )

    mesh = components.for_model(model)
    if mesh is None:
        kind = "skinned" if model.skinned else "static"
        return Skip(
            f"{model.path} is a {kind} model and no {kind} mesh component is chosen, so no "
            "prefab was generated for it -- pick one in the Model Prefabs panel"
        )

    target = f"{MODELS_DIR}/{model.stem}.prefab"
    if target in claimed:
        return Skip(f"{model.path} and another model would both generate {target}; rename one")
    if os.path.exists(layout.resolve(target)):
        return Skip(
            f"{target} already exists and is not generated, so {model.path} is left without "
            "one -- rename the hand-authored prefab if you want it generated"
        )
    claimed.add(target)
    return Create(model, target, mesh)


def list_models(layout: ProjectLayout) -> list[Model]:
    """Every identified model under ``assets/``, classified by whether it carries a rig, with the
    mesh document its sidecar records."""
    return [
        Model(
            reference,
            gltf.has_skin(layout.resolve(reference.path)),
            _mesh_document(layout, reference),
        )
        for reference in assets.list_assets(layout, list(MODEL_EXTENSIONS))
    ]


def _mesh_document(layout: ProjectLayout, model: AssetReference) -> AssetReference | None:
    """The ``.mesh`` document the model's sidecar names under ``[glb] mesh``, or ``None``."""
    meta = sidecar.read(sidecar.path_for(layout.resolve(model.path)))
    glb = meta.setting("glb") if meta else None
    return asset_reference.read(glb.get("mesh"), "glb.mesh", lambda _message: None) if glb else None


def read_generated(
    layout: ProjectLayout, mesh_fields: document_schema.MeshFields
) -> list[GeneratedPrefab]:
    """Every prefab under ``assets/`` whose root meta carries :data:`GENERATED_FROM`."""
    found: list[GeneratedPrefab] = []
    for reference in _prefabs(layout):
        absolute = layout.resolve(reference.path)
        document = _read_document(absolute, reference.path)
        if document is None:
            continue
        root = document.single_root()
        if root is None or root.meta is None:
            continue
        model_guid = root.meta.data.get(GENERATED_FROM)
        if not isinstance(model_guid, str) or not model_guid:
            continue

        path, component_id, field_name = _mesh_reference(root, mesh_fields)
        found.append(GeneratedPrefab(
            absolute, reference.path, reference.guid, model_guid, path, component_id, field_name
        ))
    return found


def referenced_prefabs(layout: ProjectLayout) -> frozenset[str]:
    """Every prefab identity some document instantiates. A generated prefab in this set is not
    deleted however long its model has been gone: the level would lose the object."""
    guids: set[str] = set()
    for reference in _prefabs(layout):
        document = _read_document(layout.resolve(reference.path), reference.path)
        if document is None:
            continue
        for entry in document.objects:
            if entry.prefab is not None:
                guids.add(entry.prefab.guid)
    return frozenset(guids)


def apply(layout: ProjectLayout, actions: list, timeout: float = sidecar.WAIT_SECONDS) -> list[str]:
    """Perform ``actions`` and report what happened, one line each.

    Every new document is written first and the whole batch is identified afterwards. The
    watcher reconciles them together, so waiting for one identity at a time turns a single
    reconcile into one timeout per model.
    """
    log: list[str] = []
    written: dict[str, str] = {}

    for action in actions:
        if isinstance(action, Skip):
            log.append(action.reason)
        elif isinstance(action, Create):
            line = _write(layout, action, written)
            if line is not None:
                log.append(line)
        elif isinstance(action, Move):
            log.append(_move(action))
        elif isinstance(action, Delete):
            log.append(_delete(action))

    if written:
        log.extend(_identify(written, timeout))
    return log


def _write(layout: ProjectLayout, action: Create, written: dict[str, str]) -> str | None:
    document = new_prefab.root_only(action.model.stem, meta={GENERATED_FROM: action.model.guid})
    document.root().components.append(PrefabComponent(
        action.mesh.component_id,
        action.mesh.type_name,
        {action.mesh.field_name: asset_reference.write(action.model.mesh)},
    ))

    absolute = layout.resolve(action.relative)
    try:
        written[os.path.abspath(absolute)] = new_prefab.write(absolute, layout, document)
    except (new_prefab.CreateError, PrefabDocumentError, OSError) as error:
        return f"could not generate {action.relative}: {error}"
    return None


def _identify(written: dict[str, str], timeout: float) -> list[str]:
    """One line per prefab, once the watcher has given the batch its identities."""
    found, missing = new_prefab.identify_all(written, timeout)
    log = [f"generated {relative}" for relative in sorted(found[path].path for path in found)]
    if missing:
        # One line for the batch: they all failed for the same reason, and twenty copies of it
        # would bury whatever else the pass reported.
        log.append(
            f"could not identify {len(missing)} new prefab(s) within {timeout:.0f}s "
            f"({missing[0]}{', …' if len(missing) > 1 else ''}); they are written but have no "
            f"{sidecar.SUFFIX} yet, so nothing can reference them. The asset watcher mints those."
        )
    return log


def _move(action: Move) -> str:
    """Repoint the mesh reference. The prefab keeps its file name: the guid is the identity and
    the path a hint, so renaming the file would move it out from under every level that already
    references it, to fix nothing."""
    prefab = action.prefab
    if prefab.mesh_component_id is None or prefab.mesh_field is None:
        return (
            f"{prefab.relative} carries no mesh reference to point at {action.model.path}; "
            f"add one, or clear its {GENERATED_FROM}"
        )

    document = _read_document(prefab.path, prefab.relative)
    component = document.root().component(prefab.mesh_component_id) if document else None
    if component is None:
        return f"could not update {prefab.relative}: its mesh component is gone"

    component.data[prefab.mesh_field] = asset_reference.write(action.model.mesh)
    try:
        atomic.write_text(prefab.path, prefab_document.dumps(document))
    except OSError as error:
        return f"could not update {prefab.relative}: {error}"
    return f"{prefab.relative} now points at {action.model.mesh.path if action.model.mesh else action.model.path}"


def _delete(action: Delete) -> str:
    try:
        os.remove(action.prefab.path)
        meta = sidecar.path_for(action.prefab.path)
        if os.path.exists(meta):
            os.remove(meta)
    except OSError as error:
        return f"could not delete {action.prefab.relative}: {error}"
    return f"deleted {action.prefab.relative}; its model is gone"


def _prefabs(layout: ProjectLayout) -> list[AssetReference]:
    return assets.list_assets(layout, [".prefab"])


def _read_document(absolute: str, relative: str):
    try:
        with open(absolute, encoding="utf-8") as handle:
            return prefab_document.loads(handle.read(), relative)
    except (OSError, PrefabDocumentError):
        return None


def _mesh_reference(root, mesh_fields: document_schema.MeshFields):
    """The mesh path a generated prefab's root names, and where it holds it."""
    for component in root.components:
        if component.id == well_known.META_ID:
            continue
        for name, value in component.data.items():
            path = value.get("path") if isinstance(value, dict) else value
            if isinstance(path, str) and mesh_fields.is_mesh_field(component.type, name, path):
                return path, component.id, name
    return None, None, None
