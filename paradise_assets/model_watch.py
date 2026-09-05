"""The Blender half of the model prefab mirror: a poll, and the choice of mesh component.

The diff and every file write live in :mod:`document.model_prefabs`; this module supplies the
clock, the project, and the report channel. It polls rather than watching the filesystem
because the authoritative watcher is ``paradise assets watch``, and a second one racing it is
what :mod:`watch` exists to prevent -- re-listing a project's sidecars answers the same question
for a few milliseconds per poll. That watcher is also what MINTS the sidecar for each prefab this
generates, so :func:`ensure_watcher` starts it before creating anything: without it every
creation blocks and then times out with no identity.

**Off unless asked.** The mirror deletes generated prefabs, so the timer runs it only when
``mirror_model_prefabs`` is on. "Generate Model Prefabs now" runs one pass regardless, which is
how to try it without arming the deletions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import bpy

from .document import model_prefabs, project, schema, sidecar

__all__ = [
    "INTERVAL",
    "Report",
    "is_running",
    "last_report",
    "mesh_choice",
    "reconcile",
    "roots",
    "start",
    "stop",
    "stop_all",
]

#: Seconds between polls. A model arrives by drag-and-drop, so the author is looking at Blender
#: when it lands and a slower poll would read as "nothing happened".
INTERVAL = 3.0

#: How long a timer pass waits for the watcher to name what it wrote: a fraction of the interval,
#: since the wait blocks the main thread and the next pass finds the prefab anyway.
TIMER_WAIT_SECONDS = 1.0

_ROOTS: set[str] = set()

#: Per project root: how long each missing model has been missing. :func:`model_prefabs.plan`
#: fences a delete on it, since a Finder move arrives as a delete and then an add.
_ABSENCES: dict[str, dict[str, model_prefabs.Absence]] = {}

#: Per project root: the last line worth showing in the panel.
_LAST: dict[str, str] = {}


@dataclass
class Report:
    """One reconcile pass, for the operator and the panel."""

    lines: list[str] = field(default_factory=list)
    created: int = 0
    moved: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0

    #: Why the pass could not run at all, if it could not.
    problem: str | None = None

    @property
    def changed(self) -> int:
        return self.created + self.moved + self.deleted

    def summary(self) -> str:
        if self.problem is not None:
            return self.problem
        counts = [
            (self.created, "generated"), (self.moved, "updated"), (self.deleted, "deleted"),
            (self.skipped, "skipped"), (self.failed, "failed"),
        ]
        parts = [f"{count} {label}" for count, label in counts if count]
        return ", ".join(parts) if parts else "model prefabs are up to date"


def mesh_choice(
    layout: project.ProjectLayout, skinned_models: bool = True
) -> tuple[model_prefabs.MeshChoice, str | None]:
    """Which components generated prefabs author their meshes into, and what is still missing.

    Never guessed: ShiningPie declares two mesh components, and the wrong one produces a prefab
    that loads, shows the mesh, and is the wrong kind of thing. ``skinned_models`` says whether
    this project has any rigged models at all -- one that has none needs no skinned component,
    and demanding one would idle a mirror with nothing to be idle about.
    """
    candidates = schema.mesh_components(layout.root)
    if not candidates:
        return model_prefabs.MeshChoice(), (
            "No game schema, so there is no mesh component to author into. Build the game's "
            "launcher once (the Play panel's Build Schema button) and try again."
        )

    from .prefs import get_preferences

    preferences = get_preferences()
    names = ", ".join(candidate.type_name for candidate in candidates)
    wanted = [
        ("static_mesh_component", "static", True),
        ("skinned_mesh_component", "skinned", skinned_models),
    ]

    chosen: dict[str, schema.MeshComponent | None] = {}
    problems: list[str] = []
    for attribute, kind, needed in wanted:
        configured = (getattr(preferences, attribute, "") or "").strip()
        if not configured:
            # One candidate is not an ambiguity for a STATIC model. It is never assumed for a
            # rigged one: a rig authored as the static component loads, shows the mesh, and is
            # the wrong kind of thing in the game, so an unset skinned picker leaves rigged models
            # unplanned (plan() skips each with a reason) while static generation goes on.
            chosen[kind] = candidates[0] if kind == "static" and len(candidates) == 1 else None
            if chosen[kind] is None and needed and kind == "static":
                problems.append(
                    f"no {kind} mesh component is chosen (this game declares {len(candidates)}: "
                    f"{names})"
                )
            continue

        match = next((c for c in candidates if c.type_name.lower() == configured.lower()), None)
        chosen[kind] = match
        if match is None:
            problems.append(f"'{configured}' is not a mesh component of this game ({names})")

    choice = model_prefabs.MeshChoice(chosen.get("static"), chosen.get("skinned"))
    if not problems:
        return choice, None
    return choice, "; ".join(problems) + ". Choose in the Model Prefabs panel."


def reconcile(layout: project.ProjectLayout, timeout: float = sidecar.WAIT_SECONDS) -> Report:
    """One pass: generate what is missing, follow what moved, delete what the model took away.

    *timeout* bounds the wait for the watcher to identify what was written. An operator waits
    the full default; the timer passes a fraction of its own interval, because that wait is on
    the main thread and a prefab the watcher has not named yet is simply found on the next pass.
    """
    report = Report()
    models = model_prefabs.list_models(layout)
    choice, problem = mesh_choice(layout, any(model.skinned for model in models))
    if problem is not None:
        report.problem = problem
        return report

    generated = model_prefabs.read_generated(layout, schema.load(layout.root))

    # Only a candidate deletion needs the reference scan, and that reads every document in the
    # project -- too much to pay on a poll where nothing is missing.
    live = {model.guid for model in models}
    referenced = (
        model_prefabs.referenced_prefabs(layout)
        if any(prefab.model_guid not in live for prefab in generated)
        else frozenset()
    )

    result = model_prefabs.plan(
        layout, choice, models, generated,
        referenced=referenced,
        absences=_ABSENCES.get(layout.root),
        now=time.monotonic(),
    )
    _ABSENCES[layout.root] = result.absences

    for action in result.actions:
        if isinstance(action, model_prefabs.Create):
            report.created += 1
        elif isinstance(action, model_prefabs.Move):
            report.moved += 1
        elif isinstance(action, model_prefabs.Delete):
            report.deleted += 1
        else:
            report.skipped += 1

    # A new prefab has no identity until `paradise assets watch` mints its sidecar, and a create
    # blocks waiting for one. Starting the watcher is therefore part of doing the work, not a
    # convenience -- without it every generation times out.
    if report.created and (blocked := ensure_watcher(layout.root)) is not None:
        report.problem = blocked
        return report

    report.lines = model_prefabs.apply(layout, result.actions, timeout)
    report.failed = sum(1 for line in report.lines if line.startswith("could not"))
    return report


def ensure_watcher(project_root: str) -> str | None:
    """Make sure the project has a watcher to mint sidecars, or say why it cannot have one."""
    from . import watch

    if watch.is_running(project_root):
        return None
    problem = watch.start(project_root)
    if problem is None:
        return None
    return (
        f"{problem} The asset watcher is what gives a new prefab its identity, so nothing can "
        "be created without it."
    )


def start(project_root: str) -> None:
    """Poll this project while a document from it is open."""
    _ROOTS.add(project_root)
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=INTERVAL, persistent=True)


def roots() -> frozenset[str]:
    """The projects being polled."""
    return frozenset(_ROOTS)


def stop(project_root: str) -> None:
    _ROOTS.discard(project_root)
    _ABSENCES.pop(project_root, None)
    if not _ROOTS:
        stop_all()


def stop_all() -> None:
    """Drop the poll. Called when the last document closes and on unregister."""
    _ROOTS.clear()
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)


def is_running() -> bool:
    return bool(_ROOTS) and bpy.app.timers.is_registered(_tick)


def last_report(project_root: str) -> str | None:
    return _LAST.get(project_root)


def remember(project_root: str, report: Report) -> None:
    """Report the way :mod:`watch` does: the console gets every line, the panel gets one."""
    if report.problem is None and not report.changed and not report.skipped:
        return
    _LAST[project_root] = report.summary()
    for line in report.lines or ([report.problem] if report.problem else []):
        print(f"[paradise_assets] {line}")


def _tick() -> float | None:
    """The timer body. Returns the next interval, or ``None`` to stop the timer."""
    if not _ROOTS:
        return None

    from .prefs import get_preferences

    preferences = get_preferences()
    if preferences is not None and preferences.mirror_model_prefabs:
        for root in sorted(_ROOTS):
            layout = project.locate(root)
            if layout is not None:
                remember(root, reconcile(layout, timeout=TIMER_WAIT_SECONDS))
    return INTERVAL
