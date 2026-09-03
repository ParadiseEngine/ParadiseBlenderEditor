"""Document identity text, mirroring C# ``DocumentGuid``: the canonical spelling is hyphenated
lowercase (what ``str(uuid.UUID)`` and .NET's ``Guid.ToString()`` both produce), and reading also
accepts the undashed form the Godot host stored so migrated scenes keep their identities. Every
reader normalises on the way in and every writer formats on the way out, so a comparison
anywhere in between is a comparison of VALUES: an instance guid spelled ``AAAA…`` in one file and
``aaaa…`` in another is one object, and a child minted from it hashes the same text C# hashes.

Not ``uuid.UUID(text)`` directly: that also accepts braces and ``urn:`` forms no tool writes, and
widening what one mirror accepts is how the two drift.
"""

from __future__ import annotations

import uuid

__all__ = ["canonical", "is_text", "parse"]

_HEX = frozenset("0123456789abcdefABCDEF")


def parse(text: object) -> uuid.UUID | None:
    """The value of *text*, or ``None`` when it is not a UUID in a form the format accepts.
    The all-zero guid parses (it is a value); whether it is ALLOWED is the caller's rule."""
    if not isinstance(text, str):
        return None
    if len(text) == 36:
        if any(text[i] != "-" for i in (8, 13, 18, 23)):
            return None
        digits = text.replace("-", "")
        if len(digits) != 32:
            return None
    elif len(text) == 32:
        digits = text
    else:
        return None
    if any(c not in _HEX for c in digits):
        return None
    return uuid.UUID(hex=digits)


def is_text(text: object) -> bool:
    """Whether *text* is a non-empty guid in an accepted spelling (C# ``TryParse`` plus the
    ``Guid.Empty`` refusal every reader applies)."""
    value = parse(text)
    return value is not None and value.int != 0


def canonical(text: str) -> str:
    """*text* in the canonical spelling. Raises ``ValueError`` for anything :func:`parse`
    refuses; a caller that has already validated may call it freely."""
    value = parse(text)
    if value is None:
        raise ValueError(f"'{text}' is not a UUID")
    return str(value)
