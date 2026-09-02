"""An asset reference (mirror of C# ``AssetReference`` / ``AssetReferenceCodec``): the GUID is
authoritative, the authoring path is the diffable fallback and the recovery route for a lost
sidecar. Wire form is the inline table ``{ guid, path }``; ``{}`` is the null slot. The guid is
validated and normalised like C# does, so a reference spelled in uppercase compares equal to the
sidecar it names and is written back canonical.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import guid as document_guid
from .canonical_toml import GUID_KEY, PATH_KEY, InlineTable

__all__ = ["AssetReference", "read", "write"]


@dataclass(frozen=True)
class AssetReference:
    """An asset's identity, and the path that identity is expected to live at."""

    guid: str
    path: str

    def __str__(self) -> str:
        return f"{self.path} ({self.guid})" if self.path else self.guid


def write(reference: AssetReference | None) -> InlineTable:
    """Renders a reference, or ``{}``. Key order guid, path is fixed: the C# side must produce
    the same bytes."""
    if reference is None:
        return InlineTable()
    if not document_guid.is_text(reference.guid) or not reference.path:
        raise ValueError(
            f"an asset reference needs both a guid and a path; got guid '{reference.guid}' "
            f"and path '{reference.path}'"
        )
    return InlineTable({GUID_KEY: document_guid.canonical(reference.guid), PATH_KEY: reference.path})


def read(value, context: str, fail) -> AssetReference | None:
    """Reads a reference from a restored document value; ``None`` when it is ``{}``. Only an
    :class:`InlineTable` qualifies: a table with any other key is not reference-shaped, and
    accepting it would drop that key on the next write."""
    if not isinstance(value, InlineTable):
        raise fail(
            f"holds {_describe(value)} where an asset reference {{ guid, path }} was expected {context}"
        )
    if len(value) == 0:
        return None

    guid = value.get(GUID_KEY)
    path = value.get(PATH_KEY)

    # A path-only reference resolves today and breaks on the first rename.
    if not isinstance(guid, str) or not isinstance(path, str):
        raise fail(f"has an asset reference missing '{GUID_KEY}' or '{PATH_KEY}' {context}")
    if not document_guid.is_text(guid):
        raise fail(
            f"holds '{guid}' where an asset reference's '{GUID_KEY}' must be a non-empty UUID {context}"
        )
    if not path:
        raise fail(f"has an asset reference with an empty '{PATH_KEY}' {context}")

    return AssetReference(document_guid.canonical(guid), path)


def _describe(value) -> str:
    if value is None:
        return "nothing"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, dict):
        return "a table"
    if isinstance(value, list):
        return "an array"
    return f"a {type(value).__name__}"
