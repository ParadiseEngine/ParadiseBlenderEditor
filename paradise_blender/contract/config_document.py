"""An authored CONFIG document -- ``Components`` of ``{"Id", "Data"}`` pairs in a file.

The same payload shape authored components travel in inside a scene export, but living in a
standalone file a game loads directly rather than on an entity.

Nothing here knows what a particular document is FOR. A project points the addon at any number of
them -- a game's tunables, a level's settings, a difficulty table -- and each is read and written
the same way, because each carries the same thing: authored payloads whose fields, units, ranges
and prose come from the ``authoring-schema.json`` the Components panel already reads. ShiningPie's
``data/shiningpie/config.json`` is the first of them; its ``shiningpie.tuning.*`` groups are
``[Authored]`` records like any other.

**Merging is surgical, and that is the whole point of this module.** A config file is written by
hand and holds more than payloads -- prose ``"// note"`` keys, and whole content sections a game
keeps to itself, none of which any editor understands. Being open to arbitrary documents makes
that rule stricter, not looser: the addon cannot know what any given key means. Rebuilding
the document from what a panel knows would silently delete all of it. :func:`merge_payloads`
therefore replaces only the ``Data`` object of the ids it was given and leaves every other key,
and the key order, exactly as it found them.

**The promise covers TOP-LEVEL keys, not the inside of a payload.** ``Data`` is replaced wholesale,
so a hand-added key within an authored component's payload -- a note inside one row of a list, say
-- does not survive a save. That has always been true of nested objects; it is stated here because
authored lists moved a body of hand-written content from the protected side of that line to the
unprotected one, and the difference should be read rather than discovered.

Nothing here imports ``bpy``: the round-trip that must not lose data is plain-``pytest`` testable.
Serialization is not here either -- callers hand the merged document to
:func:`..contract.writer.write_json_document`, so a config file is written with the contract's
float formatting and atomic replace like every other document this addon emits.
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
    """A document this reader cannot use.

    Raised rather than degraded to an empty document on purpose, and for a different reason than
    :class:`..contract.authoring.SchemaError`: a missing schema costs an author a dropdown entry,
    but a config this module misreads is one a SAVE would then overwrite with defaults. Refusing
    to load is what keeps a malformed file from becoming a destroyed file.
    """


def read(text: str) -> dict[str, Any]:
    """Parse a config document, validating only the part this addon understands.

    The whole parsed document is returned, not just the components: everything else in the file
    belongs to the game, and :func:`merge_payloads` has to hand it back untouched.

    The validation mirrors the game-side loader (ShiningPie's ``GameConfig.ReadComponents``) so
    both halves refuse the same files -- including the duplicate id, which is a typo whose
    quieter half would otherwise be silently discarded by whichever side won.
    """
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
    """The component ids the document carries, in FILE order -- which is the order a panel
    should draw them in, so the editor and the file read the same way down the page."""
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
    """The document with each named component's ``Data`` replaced, and nothing else changed.

    An id the document does not declare is APPENDED rather than ignored, so a tuning group added
    to the game's C# reaches the file the first time it is saved.

    Copies rather than mutating: the caller still holds the document it read, and a save that
    fails partway through must not leave that copy half-updated.
    """
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
    """(mtime_ns, size) for change detection, ``(0, 0)`` when the file is not there.

    The same cheap stamp :func:`..contract.authoring.schema_stamp` uses, for a different question:
    not "reload the schema" but "the file moved under the editor, so saving would clobber whatever
    moved it".
    """
    try:
        stat = os.stat(path)
    except OSError:
        return (0, 0)
    return (stat.st_mtime_ns, stat.st_size)


#: Files larger than this are not read during discovery. A config document is a page of numbers;
#: anything bigger under ``data/`` is a scene export or a baked table, and parsing every one of
#: them to find out would make the picker crawl on a real project.
MAX_SCAN_BYTES = 4 * 1024 * 1024


def looks_like_config(document: Any) -> bool:
    """Whether a parsed document is one this addon can edit.

    Stricter than :func:`read` accepts, and deliberately so: ``read`` tolerates a document with no
    ``Components`` at all (a game may keep content sections in the same file), but the PICKER must
    not offer every JSON file under ``data/`` as a config. A scene export, a material document and
    the authoring schema itself all parse fine and would all be empty, confusing rows.

    The test is a non-empty ``Components`` array of ``{"Id", "Data"}`` entries -- note the exact
    casing, which is what separates a config's ``Components`` from the authoring schema's own
    lowercase ``components`` list of field declarations.
    """
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
    """Every config document under a data directory, as data-relative paths, sorted.

    Sorted so the picker's order does not depend on how the filesystem happens to walk, and
    data-relative so what a .blend stores keeps working in someone else's checkout -- the same
    rule every other contract path follows.

    Unreadable and unparseable files are skipped silently. This runs to populate a dropdown, not
    to validate a project, and a warning per stray file would fire on every open.
    """
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
