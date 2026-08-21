"""The stable ids the engine's own authored components travel under.

A transcription of ``Paradise.Export.Data.ParadiseComponentIds`` (``ParadiseComponentIds.cs`` in
the engine), which is the source of truth. Vendored for the same reason
:data:`.authoring.read_engine_schema`'s JSON is: the constants live in a C# assembly this Python
host cannot load.

**Nothing keeps these in sync mechanically.** The guard is a unit test asserting that every id in
the committed ``engine_authoring_schema.json`` appears here -- so a regenerated schema that adds
or moves a component fails loudly rather than leaving a constant pointing at nothing.

Canonical form is what the engine writes and reads: lowercase, hyphenated, 36 characters, no
braces (.NET's ``"D"``). System.Text.Json's Guid converter accepts ONLY that shape -- the 32-char
and braced spellings throw on read -- so these strings go onto the wire unchanged.
"""

from __future__ import annotations

__all__ = [
    "AGENT",
    "AUDIO_EMITTER",
    "COLLIDER",
    "ENGINE_IDS",
    "IDENTITY",
    "INTERACTABLE",
    "LIGHT",
    "PARTICLE_EMITTER",
    "RENDERABLE",
    "RIGIDBODY",
    "SPRITE_ANIMATION",
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

#: Every id above. What "is this the engine's component or the game's?" asks -- a question that
#: used to be answerable by testing for a ``paradise.`` prefix, which a GUID does not have.
ENGINE_IDS = frozenset({
    IDENTITY,
    RENDERABLE,
    COLLIDER,
    RIGIDBODY,
    AGENT,
    INTERACTABLE,
    SPRITE_ANIMATION,
    PARTICLE_EMITTER,
    AUDIO_EMITTER,
    LIGHT,
})
