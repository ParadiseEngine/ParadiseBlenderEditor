"""The component ids this host has something specific to do with.

These are the ids the engine's ``LevelDocument.cs`` records carried up to contract v5. Since v6
the engine declares NO authored components, so an id below means something only if the GAME
declares a component under it; exporting one the game does not declare fails in
:func:`engine_type_name`, and :func:`check_engine_ids` reports the rest at export. Identity and
placement are not here at all: they are the format's own ``meta`` / ``transform`` payloads
(``well_known.py``), written for every object. Tabulated rather than read because no document
states which component this host MEANS by ``RENDERABLE``; the type NAME is read off the schema
instead (:func:`engine_type_name`), since a second hand-written copy of something the schema
states is a thing that can drift.

**Deliberately NOT a complete mirror.** A constant belongs here only when this host must do
something SPECIFIC with that component -- derive it from Blender data, back it with a pointer
collection, draw it a particular way. A component this host treats like any other needs no entry,
and adding one for completeness would be maintenance bought with nothing.

There is deliberately no "is this the engine's component" set either. There was one, and it went
with the two-tier contract that gave it meaning: every component now travels the same way, so
"whose is it" stopped being a question anything needs to ask.

**Nothing keeps the constants below in sync mechanically.** The guard is :func:`check_engine_ids`,
run at export against the schema the LAUNCHER dumped: a component the engine renamed or dropped is
reported instead of leaving a constant pointing at nothing. It used to be a unit test against a
vendored copy of the engine's schema; that copy is gone, and checking against the engine the game
is actually built with is the stronger test.

Canonical form is what the engine writes and reads: lowercase, hyphenated, 36 characters, no
braces (.NET's ``"D"``). System.Text.Json's Guid converter accepts ONLY that shape -- the 32-char
and braced spellings throw on read -- so these strings go onto the wire unchanged.
"""

from __future__ import annotations

__all__ = [
    "AGENT",
    "AUDIO_EMITTER",
    "COLLIDER",
    "ENVIRONMENT",
    "LIGHT",
    "MATERIALS",
    "PARTICLE_EMITTER",
    "RENDERABLE",
    "RIGIDBODY",
    "SPRITE_ANIMATION",
    "check_engine_ids",
    "engine_type_name",
]

#: The scene's lighting and environment, written on an object of its own.
ENVIRONMENT = "f5f4a867-fe27-426a-82f2-1a2de5aceb2f"

#: The material slots overriding a mesh's own. Host-DERIVED like the name and the transform: a
#: material assignment is Blender's material slots, not something an author types, so no picker
#: could author it and no game can own it.
MATERIALS = "bdc4fc87-d7b4-41f1-bc90-fc827005adfc"

RENDERABLE = "f2c0357e-94dd-4a5a-9803-518066cb54b2"
COLLIDER = "e1cd1bc8-86f2-4225-adc9-4a324c70ebf9"
RIGIDBODY = "b7ab4dd8-c8da-4dc2-9e5e-192fd74deb11"
AGENT = "5801915b-3d0c-4940-8970-7d1487b991cf"
SPRITE_ANIMATION = "d3e53cd4-89c6-4ca8-851e-7596da889c68"
PARTICLE_EMITTER = "1b4d1bdd-dea1-4b86-9b6a-879c46346b9e"
AUDIO_EMITTER = "e6ec7f42-df09-4ec9-af06-128ddf3eda8e"
LIGHT = "fc886b84-c48c-4415-afd9-b03d6faf5ab7"

def engine_type_name(component_id: str, data_dir: str) -> str:
    """The fully qualified CLR name the engine publishes for one of its components.

    Read off the GAME's dumped schema, which is now the only schema this host has. That works
    because the document is dumped from the launcher, and a launcher built with
    ``ParadiseAuthoringScanReferences`` merges every assembly it references — so the engine's own
    components are in there, at full fidelity, described by the engine the game actually builds
    against. Better than the vendored copy this replaced, which could disagree with that engine
    and, being merged first, would have won.

    Still not tabulated here beside the ids: the schema states it, and a second hand-written copy
    is a thing that can drift. The ids above have no such source — no document states which
    component this host means by ``RENDERABLE`` — which is why they are transcribed and this is
    not.

    RAISES when the schema cannot answer, rather than omitting the field. The name is written onto
    every exported payload beside the id, and the engine reads it only when the id fails to
    resolve — so a missing one is merely a worse diagnostic, and it would be tempting to skip it.
    That would make the EXPORT depend on whether the game had been built: the same .blend would
    produce two different ``data/scenes/*.json``, one of which gets committed. An export is
    reproducible or it is not.
    """
    from . import authoring

    for component in authoring.schema_for_data_dir(data_dir).components:
        if component.id == component_id:
            return component.type

    reason = authoring.schema_load_error(data_dir)
    raise KeyError(
        f"The authoring schema in '{data_dir}' does not describe engine component "
        f"{component_id}, so it cannot be exported. "
        + (reason or
           "The schema is present but does not carry the engine's components — build the game's "
           "LAUNCHER, and check it sets ParadiseAuthoringScanReferences (only the project that "
           "references the whole game dumps a document describing all of it).")
    )


def check_engine_ids(data_dir: str) -> list[str]:
    """Every constant above that the loaded schema does not corroborate.

    The drift guard, and it replaces a unit test. Nothing keeps the constants above in step
    with what a game declares; the old guard asserted each one against a vendored engine schema,
    which died with the vendored schema. Checking against the schema the game is actually built
    against is the stronger test. A v6 game that reuses none of these ids gets one warning per
    constant, which is the honest report: this host derives those components and that game
    cannot receive them.

    Returns messages rather than raising: a drifted id is worth SAYING at export, but it is not
    worth refusing to export over. The one that must be fatal — a component this host is actively
    writing and cannot name — is already fatal in :func:`engine_type_name`.
    """
    from . import authoring

    document = authoring.schema_for_data_dir(data_dir)
    if not document.components:
        return []  # nothing loaded; schema_load_error already explains that, once.

    known = {component.id for component in document.components}
    missing = []
    for name, value in sorted(vars(_module()).items()):
        if not name.isupper() or not isinstance(value, str) or name.startswith("_"):
            continue
        if value not in known:
            missing.append(
                f"{name} ({value}) is not in the authoring schema — the engine may have renamed "
                "or dropped that component since this constant was transcribed.")
    return missing


def _module():
    import sys

    return sys.modules[__name__]
