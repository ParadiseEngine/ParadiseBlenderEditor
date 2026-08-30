"""A reference from an authored document to an asset -- the Python mirror of C# ``AssetReference``.

**The GUID is authoritative and the path is the fallback.** Resolution tries the GUID first, so
renaming or moving an asset never touches a document that references it. The path is kept because
a GUID alone is unreadable in a diff and, more importantly, because it is the recovery route: a
sidecar that gets lost would otherwise break every reference to its asset.

Both are written, and ``verify`` refuses a document where the two name DIFFERENT assets.

``path`` is always the AUTHORING path (``materials/x.toml``), never the built one. The build
flattens a reference to whatever value the runtime resolves -- the asymmetry the export contract
already lives by: *authored as a REFERENCE, exported as a VALUE*.

The wire form is an inline table, ``{ guid = "…", path = "…" }``, and an absent reference is the
empty one, ``{}`` -- which is what a null material slot is, and why it matters that the empty form
exists at all.
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
    """Renders a reference, or ``{}`` for ``None``.

    Key order is fixed at guid then path -- guid first because it is the authoritative half, and
    fixed because the canonical writer emits model order and the C# side has to produce the same
    bytes.
    """
    if reference is None:
        return InlineTable()
    return InlineTable({GUID_KEY: reference.guid, PATH_KEY: reference.path})


def read(value, context: str, fail) -> AssetReference | None:
    """Reads a reference from a document value; ``None`` when it is ``{}``."""
    if not isinstance(value, dict):
        raise fail(f"holds {type(value).__name__} where an asset reference {{ guid, path }} was expected {context}")
    if len(value) == 0:
        return None

    guid = value.get(GUID_KEY)
    path = value.get(PATH_KEY)

    # Both keys required whenever the table is non-empty. A reference carrying only a path would
    # resolve today and break on the first rename -- the failure the guid exists to prevent, so
    # accepting it would quietly give up the guarantee.
    if not isinstance(guid, str) or not isinstance(path, str):
        raise fail(f"has an asset reference missing '{GUID_KEY}' or '{PATH_KEY}' {context}")
    if not guid or not path:
        raise fail(f"has an asset reference with an empty '{GUID_KEY}' or '{PATH_KEY}' {context}")

    return AssetReference(guid, path)
