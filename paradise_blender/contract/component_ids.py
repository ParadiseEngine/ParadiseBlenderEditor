"""The engine component ids this host names.

Transcribed from the ``[Guid]`` attribute on each record in ``LevelDocument.cs``, which is the
source of truth -- the engine's own registry, schema and router all read that attribute and
nothing else. (There was a ``ParadiseComponentIds`` table holding a second copy; it was deleted
with the v4 contract, since a second copy of an identity is a thing that can disagree with the
first.) Vendored for the same reason :data:`.authoring.read_engine_schema`'s JSON is: the
constants live in a C# assembly this Python host cannot load.

**Deliberately NOT a complete mirror.** A constant belongs here only when this host must do
something SPECIFIC with that component -- derive it from Blender data, back it with a pointer
collection, draw it a particular way. A component this host treats like any other needs no entry,
and adding one for completeness would be maintenance bought with nothing.

There is deliberately no "is this the engine's component" set either. There was one, and it went
with the two-tier contract that gave it meaning: every component now travels the same way, so
"whose is it" stopped being a question anything needs to ask.

**Nothing keeps the constants below in sync mechanically.** The guard is a unit test asserting
each one still appears in the committed ``engine_authoring_schema.json``, so a component the
engine renames or drops fails loudly instead of leaving a constant pointing at nothing.

Canonical form is what the engine writes and reads: lowercase, hyphenated, 36 characters, no
braces (.NET's ``"D"``). System.Text.Json's Guid converter accepts ONLY that shape -- the 32-char
and braced spellings throw on read -- so these strings go onto the wire unchanged.
"""

from __future__ import annotations

import functools

__all__ = [
    "AGENT",
    "AUDIO_EMITTER",
    "COLLIDER",
    "IDENTITY",
    "INTERACTABLE",
    "LIGHT",
    "PARTICLE_EMITTER",
    "RENDERABLE",
    "RIGIDBODY",
    "SPRITE_ANIMATION",
    "engine_type_name",
]

#: Entity-level identity. Routed onto the entity itself rather than into its components -- it is
#: what the entity IS, not something it has.
IDENTITY = "0c068bf4-495f-495b-be8d-9b02042a41c2"

RENDERABLE = "f2c0357e-94dd-4a5a-9803-518066cb54b2"
COLLIDER = "e1cd1bc8-86f2-4225-adc9-4a324c70ebf9"
RIGIDBODY = "b7ab4dd8-c8da-4dc2-9e5e-192fd74deb11"
AGENT = "5801915b-3d0c-4940-8970-7d1487b991cf"
INTERACTABLE = "0283ee5f-775b-412b-a91c-03ecd9b61165"
SPRITE_ANIMATION = "d3e53cd4-89c6-4ca8-851e-7596da889c68"
PARTICLE_EMITTER = "1b4d1bdd-dea1-4b86-9b6a-879c46346b9e"
AUDIO_EMITTER = "e6ec7f42-df09-4ec9-af06-128ddf3eda8e"
LIGHT = "fc886b84-c48c-4415-afd9-b03d6faf5ab7"

@functools.cache
def engine_type_name(component_id: str) -> str:
    """The fully qualified CLR name the engine publishes for one of its components.

    Read off the vendored schema rather than tabulated here: the schema already states it, and a
    hand-written copy would be a second thing to keep in step. It is written onto
    every exported payload beside the id -- the engine reads it only when the id fails to resolve,
    but a payload without it is a bare GUID and diagnoses nothing.
    """
    from . import authoring

    for component in authoring.read_engine_schema().components:
        if component.id == component_id:
            return component.type
    raise KeyError(f"{component_id} is not an engine component")
