"""An authored config document: ``Components`` of ``{"Id", "Data"}`` pairs in a hand-edited file.

Merging is surgical: the file holds prose keys and content sections no editor understands, so
:func:`merge_payloads` replaces only the ``Data`` of the ids it is given and leaves every other
key and the key order as found. ``Data`` itself is replaced wholesale, so a hand-added key inside
a payload (a note in a list row) does not survive a save. No ``bpy`` import.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

__all__ = [
    "COMPONENTS_KEY",
    "MAX_SCAN_BYTES",
    "ConfigError",
    "config_stamp",
    "declared_ids",
    "discover",
    "looks_like_config",
    "merge_payloads",
    "payload_of",
    "read",
]

COMPONENTS_KEY = "Components"
ID_KEY = "Id"
DATA_KEY = "Data"


class ConfigError(ValueError):
    """Raised, never degraded to empty: a misread config is one a save would overwrite with
    defaults."""


def read(text: str) -> dict[str, Any]:
    """Parse a config document, returning the WHOLE document so the rest can be handed back.
    Validation mirrors ShiningPie's ``GameConfig.ReadComponents`` so both halves refuse the
    same files, duplicate ids included."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise ConfigError(f"Config is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise ConfigError("Config document must be a JSON object.")

    entries = document.get(COMPONENTS_KEY)
    if entries is None:
        return document
    if not isinstance(entries, list):
        raise ConfigError(f"Config '{COMPONENTS_KEY}' must be an array.")

    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError(f"Every '{COMPONENTS_KEY}' entry must be a JSON object.")
        component_id = entry.get(ID_KEY)
        if not isinstance(component_id, str) or not component_id:
            raise ConfigError(f"Every '{COMPONENTS_KEY}' entry needs a non-empty '{ID_KEY}'.")
        if component_id in seen:
            raise ConfigError(f"Config declares '{component_id}' twice.")
        seen.add(component_id)
        if not isinstance(entry.get(DATA_KEY), dict):
            raise ConfigError(f"Config component '{component_id}' needs a '{DATA_KEY}' object.")

    return document


def declared_ids(document: Mapping[str, Any]) -> list[str]:
    """The component ids in file order."""
    return [entry[ID_KEY] for entry in document.get(COMPONENTS_KEY) or []]


def payload_of(document: Mapping[str, Any], component_id: str) -> dict[str, Any] | None:
    """One component's ``Data``, or None when the document does not declare it."""
    for entry in document.get(COMPONENTS_KEY) or []:
        if entry.get(ID_KEY) == component_id:
            return entry.get(DATA_KEY)
    return None


def merge_payloads(
    document: Mapping[str, Any], updates: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """A copy with each named component's ``Data`` replaced (undeclared ids appended) and nothing
    else changed. A copy, so a save that fails partway leaves the caller's document intact."""
    merged = dict(document)
    entries = [dict(entry) for entry in document.get(COMPONENTS_KEY) or []]

    remaining = dict(updates)
    for entry in entries:
        payload = remaining.pop(entry.get(ID_KEY), None)
        if payload is not None:
            entry[DATA_KEY] = dict(payload)

    for component_id, payload in remaining.items():
        entries.append({ID_KEY: component_id, DATA_KEY: dict(payload)})

    merged[COMPONENTS_KEY] = entries
    return merged


def config_stamp(path: str) -> tuple[int, int]:
    """(mtime_ns, size), ``(0, 0)`` when absent."""
    try:
        stat = os.stat(path)
    except OSError:
        return (0, 0)
    return (stat.st_mtime_ns, stat.st_size)


#: Discovery skips bigger files: parsing every scene export to find configs makes the picker crawl.
MAX_SCAN_BYTES = 4 * 1024 * 1024


def looks_like_config(document: Any) -> bool:
    """Whether the picker should offer this document: a non-empty ``Components`` array (exact
    casing; the authoring schema's lowercase ``components`` is not one). Stricter than
    :func:`read`, or every JSON under ``data/`` would be an empty, confusing row."""
    if not isinstance(document, dict):
        return False
    entries = document.get(COMPONENTS_KEY)
    if not isinstance(entries, list) or not entries:
        return False
    return all(
        isinstance(entry, dict)
        and isinstance(entry.get(ID_KEY), str)
        and entry.get(ID_KEY)
        and isinstance(entry.get(DATA_KEY), dict)
        for entry in entries
    )


def discover(data_dir: str) -> list[str]:
    """Every config document under ``data/``, data-relative and sorted. Unparseable files are
    skipped silently: this populates a dropdown, and a warning per stray file would fire on
    every open."""
    found: list[str] = []
    for root, _, names in os.walk(data_dir):
        for name in names:
            if not name.endswith(".json"):
                continue
            full = os.path.join(root, name)
            try:
                if os.path.getsize(full) > MAX_SCAN_BYTES:
                    continue
                with open(full, encoding="utf-8") as file:
                    document = json.load(file)
            except (OSError, ValueError):
                continue
            if looks_like_config(document):
                found.append(os.path.relpath(full, data_dir).replace(os.sep, "/"))
    return sorted(found)
