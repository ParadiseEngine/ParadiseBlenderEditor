"""An asset reference (mirror of C# ``AssetReference``): the GUID is authoritative, the
authoring path is the diffable fallback and the recovery route for a lost sidecar. Wire form is
the inline table ``{ guid, path }``; ``{}`` is the null slot. KNOWN GAP (#30): the guid text is
not validated or normalised here as C# does.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    return InlineTable({GUID_KEY: reference.guid, PATH_KEY: reference.path})


def read(value, context: str, fail) -> AssetReference | None:
    """Reads a reference from a document value; ``None`` when it is ``{}``."""
    if not isinstance(value, dict):
        raise fail(
            f"holds {type(value).__name__} where an asset reference {{ guid, path }} was expected {context}"
        )
    if len(value) == 0:
        return None

    guid = value.get(GUID_KEY)
    path = value.get(PATH_KEY)

    # A path-only reference resolves today and breaks on the first rename.
    if not isinstance(guid, str) or not isinstance(path, str):
        raise fail(f"has an asset reference missing '{GUID_KEY}' or '{PATH_KEY}' {context}")
    if not guid or not path:
        raise fail(f"has an asset reference with an empty '{GUID_KEY}' or '{PATH_KEY}' {context}")

    return AssetReference(guid, path)
