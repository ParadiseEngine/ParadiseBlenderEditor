"""Reading ``<asset>.meta``, the sidecar that carries an asset's identity.

**This addon does not mint identities.** ``paradise assets watch`` runs the C#
``SidecarMaintainer``, which writes a sidecar for any file under ``assets/`` that lacks one, on
reconcile and on every file event. Two minters would race, and the loser's guid is dropped with
a ``Conflicted`` log line -- so the addon writes a document and then WAITS for the identity to
appear (:func:`wait_for`). That is why there is no ``write`` or ``mint`` here.

Two deliberate divergences from C# ``SidecarMeta.Parse``, both because the only question this
reader answers is "does this asset have an identity", while C# also REWRITES sidecars:

- A missing ``schema_version`` is accepted (C# requires it). Older mints and hand-written test
  fixtures omit it, and the identity in them is not in doubt.
- A stray scalar at the root -- a legacy ``kind = "document"`` -- is ignored (C# refuses the
  whole document, because its next rewrite would drop the key). Refusing here would cost the
  asset every reference to it: no catalogue entry, no pickable reference, invisible to the model
  mirror. A DECLARED ``schema_version`` this build cannot read is still a refusal.
"""

from __future__ import annotations

import time
import tomllib
from dataclasses import dataclass, field

from . import guid as document_guid

__all__ = [
    "STRUCTURAL_KEYS",
    "SUFFIX",
    "SUPPORTED_SCHEMA_VERSION",
    "WAIT_SECONDS",
    "Sidecar",
    "path_for",
    "read",
    "wait_for",
]

SUPPORTED_SCHEMA_VERSION = 1

SUFFIX = ".meta"

#: Root keys that are the sidecar's own structure rather than a settings domain.
STRUCTURAL_KEYS = frozenset({"schema_version", "guid", "hash"})

#: How long to wait for the watcher to identify a file the addon has just written. Generous:
#: the maintainer reconciles on a file event within milliseconds, but the watcher may be
#: mid-build when the write lands, and giving up early means reporting failure for a prefab
#: that is about to be perfectly fine.
WAIT_SECONDS = 10.0


@dataclass(frozen=True)
class Sidecar:
    """One sidecar: the asset's identity and its opaque import-settings domains."""

    guid: str
    settings: dict[str, dict] = field(default_factory=dict)

    def setting(self, domain: str) -> dict | None:
        return self.settings.get(domain)


def path_for(asset_path: str) -> str:
    """The sidecar beside ``asset_path``."""
    return asset_path + SUFFIX


def read(path: str) -> Sidecar | None:
    """The sidecar at ``path``, or ``None`` when it is missing, unreadable, or not one.

    Unreadable reads as absent on purpose: every caller is asking whether an asset has an
    identity, and a half-written file during the watcher's mint is not an error worth failing a
    scene load over -- it is a "not yet", which is exactly what :func:`wait_for` waits through.
    """
    try:
        with open(path, "rb") as handle:
            root = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None

    version = root.get("schema_version", SUPPORTED_SCHEMA_VERSION)
    if version != SUPPORTED_SCHEMA_VERSION:
        return None

    guid = root.get("guid")
    if not document_guid.is_text(guid):
        return None

    settings = {
        key: value for key, value in root.items()
        if key not in STRUCTURAL_KEYS and isinstance(value, dict)
    }
    return Sidecar(document_guid.canonical(guid), settings)


def wait_for(asset_path: str, timeout: float = WAIT_SECONDS, interval: float = 0.1) -> Sidecar | None:
    """Block until the watcher has identified ``asset_path``, or ``None`` if it never does.

    Polling, not a filesystem event: the writer is another process and the only thing that
    matters is that the file is there AND parses -- a sidecar caught mid-write reads as absent
    and is simply waited through.
    """
    path = path_for(asset_path)
    deadline = time.monotonic() + timeout
    while True:
        found = read(path)
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)
