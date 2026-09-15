"""Per-clip root-motion settings, authored into a GLB's ``[glb]`` sidecar domain.

The engine reads per-clip import settings from ``<model>.glb.meta`` (the ``[glb]`` domain --
the same place ``optimize`` lives), keyed by the clip's **glTF animation index** because that
survives a rename in the DCC where a name does not. Each entry is one inline table::

    [glb]
    clips = [ { index = 2, name = "Walk_Loop", root_motion = true, root_bone = "root" } ]

``root_motion`` opts the clip in -- absent or ``false`` keeps the runtime's default, so an
unflagged clip and a never-touched GLB are the same thing. ``root_bone`` names the joint whose
motion is lifted onto the actor; absent means auto-detect, the skin's root joint. ``name`` is
bookkeeping, the readable stem the engine records for its clip documents: it is refreshed from
the GLB on every write so a rename cannot leave a setting labelled with a clip that is gone.

Storage is the SIDECAR, not the ``.blend``: the working file is a disposable view, so an ID
property on an imported Action would not survive the next Recreate -- and the GLB's own
``extras`` would ask the artist to re-export to change a flag. The sidecar is tooling-owned
and travels with the asset (``paradise assets mv`` keeps the pair), which is the same reason
the engine puts ``optimize`` there.

This module writes a domain of an EXISTING sidecar; it still mints no identities. A missing
or unparseable ``.meta`` is a refusal, never a rewrite -- ``sidecar.read``'s "absent reads as
not yet" applies, and clobbering a file we cannot parse would eat hand-edited domains.

Imports no ``bpy``: the merge rules are unit-tested without Blender.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass

from . import atomic, canonical_toml, gltf, sidecar
from . import guid as document_guid

__all__ = [
    "ClipRow",
    "ClipSetting",
    "ClipSettingsError",
    "ClipView",
    "Rig",
    "read_settings",
    "rig",
    "set_root_bone",
    "set_root_motion",
    "view",
]

#: The domain and its keys, spelled as the engine's GlbImportSettings/ExtractionRecord spell
#: theirs: index + name is the `NamedReference` keying extracted clip documents already use.
DOMAIN = "glb"
CLIPS_KEY = "clips"
INDEX_KEY = "index"
NAME_KEY = "name"
ROOT_MOTION_KEY = "root_motion"
ROOT_BONE_KEY = "root_bone"


class ClipSettingsError(Exception):
    """A refusal the operator reports as-is: it already names the clip, bone or file."""


@dataclass(frozen=True)
class Rig:
    """What a GLB's JSON chunk says about its clips and skin. No binary chunk is touched."""

    #: Animation names in glTF order -- the index IS the setting's key. "" where a clip is
    #: unnamed (glTF names are optional).
    clips: tuple[str, ...]
    #: Joint node names across the GLB's skins, in skin order.
    joints: tuple[str, ...]
    #: The joint root motion auto-detects to: the skin's root joint.
    root_joint: str | None


@dataclass(frozen=True)
class ClipSetting:
    """One clip's authored settings. ``root_bone = ""`` is auto-detect, not a name."""

    index: int
    name: str
    root_motion: bool = False
    root_bone: str = ""

    @property
    def is_default(self) -> bool:
        """A default entry is not written: absent and off are the same to the runtime."""
        return not self.root_motion and not self.root_bone


@dataclass(frozen=True)
class ClipRow:
    """One clip as the panel shows it: current GLB name, its setting, its own problems."""

    index: int
    name: str
    setting: ClipSetting
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClipView:
    """Everything the clips section draws, precomputed so ``draw`` never parses a file."""

    clips: tuple[ClipRow, ...]
    joints: tuple[str, ...]
    root_joint: str | None
    #: Whether the GLB's sidecar exists and carries an identity -- the write prerequisite.
    identified: bool
    problems: tuple[str, ...] = ()


#: path -> (mtime_ns, size, Rig | None). Panel draws poll at redraw rate; a GLB's clip table
#: changes only when the file does, which is exactly what the stamp sees.
_RIG_CACHE: dict[str, tuple[int, int, Rig | None]] = {}

#: sidecar path -> (stamp, parsed model | None). None covers missing AND unparseable; the
#: writer re-reads uncached so a refusal cannot be written over a file that just arrived.
_META_CACHE: dict[str, tuple[str, dict | None]] = {}


def rig(path: str) -> Rig | None:
    """The GLB's clips and skin joints, or ``None`` when the file is not a readable GLB."""
    try:
        stat = os.stat(path)
    except OSError:
        return None

    cached = _RIG_CACHE.get(path)
    if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
        return cached[2]

    found = _rig_of(gltf.read_json(path))
    _RIG_CACHE[path] = (stat.st_mtime_ns, stat.st_size, found)
    return found


def _rig_of(document: dict) -> Rig | None:
    if not document:
        return None

    animations = document.get("animations")
    clips = (
        tuple(entry.get("name", "") if isinstance(entry, dict) else "" for entry in animations)
        if isinstance(animations, list)
        else ()
    )

    nodes = document.get("nodes")
    names = [
        node.get("name", "") if isinstance(node, dict) else "" for node in nodes
    ] if isinstance(nodes, list) else []

    parent: dict[int, int] = {}
    if isinstance(nodes, list):
        for index, node in enumerate(nodes):
            children = node.get("children") if isinstance(node, dict) else None
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, int) and not isinstance(child, bool):
                        parent[child] = index

    joints: list[str] = []
    joint_indices: set[int] = set()
    root_joint = None
    skins = document.get("skins")
    if isinstance(skins, list):
        for skin in skins:
            members = skin.get("joints") if isinstance(skin, dict) else None
            if not isinstance(members, list):
                continue
            for member in members:
                if (
                    isinstance(member, int)
                    and not isinstance(member, bool)
                    and 0 <= member < len(names)
                    and member not in joint_indices
                ):
                    joint_indices.add(member)
                    if names[member]:
                        joints.append(names[member])
            # The root is the first joint whose parent is not itself a joint -- equivalently
            # skins[].joints[0] on a rig written by Blender's exporter, which orders the
            # hierarchy root first.
            if root_joint is None:
                for member in members:
                    if member in joint_indices and parent.get(member) not in joint_indices:
                        root_joint = names[member]
                        break

    return Rig(clips, tuple(joints), root_joint)


def view(glb_path: str) -> ClipView | None:
    """The clips section for ``glb_path``, or ``None`` when it has no clips to show.

    ``None`` is also the answer for an unreadable GLB: the loader already warned about the
    mesh, and a panel section that cannot list clips says nothing it can act on.
    """
    info = rig(glb_path)
    if info is None or not info.clips:
        return None

    root = _read_meta_cached(sidecar.path_for(glb_path))
    identified = root is not None and document_guid.is_text(root.get("guid"))
    stored, orphans = _collect(_stored_entries(root), info.clips)

    problems: list[str] = []
    for entry in orphans:
        problems.append(
            f"settings for clip {entry.index} ('{entry.name or 'unnamed'}') are stale — the "
            f"GLB has {len(info.clips)} clip(s) now; editing any clip drops them"
        )

    rows: list[ClipRow] = []
    for index, name in enumerate(info.clips):
        setting = stored.get(index, ClipSetting(index, name))
        label = name or f"clip {index}"
        row_problems: list[str] = []
        if setting.name and setting.name != name:
            row_problems.append(
                f"clip {index}: recorded as '{setting.name}', now '{label}' — "
                "the setting follows the clip's index"
            )
        if setting.root_bone and setting.root_bone not in info.joints:
            row_problems.append(
                f"clip '{label}': root bone '{setting.root_bone}' is not a joint of this "
                "GLB's skin"
            )
        if setting.root_motion and not info.joints:
            row_problems.append(
                f"clip '{label}' is root-motion enabled but the GLB has no skin — there is "
                "no root bone to take the motion from"
            )
        problems.extend(row_problems)
        rows.append(ClipRow(index, name, setting, tuple(row_problems)))

    return ClipView(tuple(rows), info.joints, info.root_joint, identified, tuple(problems))


def read_settings(meta_path: str) -> dict[int, ClipSetting]:
    """The sidecar's clip settings by clip index, as stored -- no clip table to reconcile
    against here, so names come from the file. Empty when there is no readable sidecar."""
    loaded = _read_meta(meta_path)
    if loaded is None:
        return {}
    return _parse_entries(_stored_entries(loaded[1]))


def set_root_motion(glb_path: str, index: int, enabled: bool) -> None:
    """Flag clip ``index`` as driving the actor's root (or back to in-place posing)."""
    _apply(glb_path, index, root_motion=enabled)


def set_root_bone(glb_path: str, index: int, bone: str) -> None:
    """Set clip ``index``'s root bone; ``""`` returns it to the skin's root joint."""
    _apply(glb_path, index, root_bone=bone)


def _apply(
    glb_path: str,
    index: int,
    *,
    root_motion: bool | None = None,
    root_bone: str | None = None,
) -> None:
    info = rig(glb_path)
    glb_name = os.path.basename(glb_path)
    if info is None:
        raise ClipSettingsError(f"{glb_name} is not a readable GLB")
    if not 0 <= index < len(info.clips):
        raise ClipSettingsError(
            f"{glb_name} has {len(info.clips)} clip(s); there is no clip {index}")
    if root_bone:
        if not info.joints:
            raise ClipSettingsError(
                f"clip '{info.clips[index] or index}': {glb_name} has no skin to pick a "
                "root bone from"
            )
        if root_bone not in info.joints:
            raise ClipSettingsError(
                f"clip '{info.clips[index] or index}': '{root_bone}' is not a joint of "
                f"{glb_name}'s skin"
            )

    meta_path = sidecar.path_for(glb_path)
    loaded = _read_meta(meta_path)
    if loaded is None or not document_guid.is_text(loaded[1].get("guid")):
        raise ClipSettingsError(
            f"{os.path.basename(meta_path)} has no identity yet — the watcher mints it; "
            "start the watch or let it finish")
    original, root = loaded

    domain = root.get(DOMAIN)
    if isinstance(domain, canonical_toml.InlineTable):
        # An inline-spelled domain (`glb = { … }`) must become a plain table before `clips`
        # lands in it: an inline table cannot hold an array of tables.
        domain = dict(domain)
        root[DOMAIN] = domain
    elif domain is not None and not isinstance(domain, dict):
        raise ClipSettingsError(
            f"[{DOMAIN}] in {os.path.basename(meta_path)} is not a table — fix it by hand "
            "rather than have this write guess")

    merged, _orphans = _collect(_stored_entries(root), info.clips)

    current = merged.get(index, ClipSetting(index, info.clips[index]))
    updated = ClipSetting(
        index,
        info.clips[index],
        current.root_motion if root_motion is None else root_motion,
        current.root_bone if root_bone is None else root_bone,
    )
    if updated.is_default:
        merged.pop(index, None)
    else:
        merged[index] = updated

    _write_domain(meta_path, original, root, domain, merged, info.clips)


def _write_domain(
    meta_path: str, original: str, root: dict, domain,
    merged: dict[int, ClipSetting], clip_names,
) -> None:
    """Swap the domain's clip list for ``merged`` and write the sidecar back canonically.

    Everything the edit did not touch passes through: structural keys, other domains, and the
    other ``[glb]`` members. Entries are written sorted by index so the file's order is the
    GLB's own -- a diff between two writes then means a setting changed, not a reorder.
    """
    if merged:
        if not isinstance(domain, dict):
            domain = {}
            root[DOMAIN] = domain
        def _entry(setting: ClipSetting) -> canonical_toml.InlineTable:
            items = [
                (INDEX_KEY, setting.index),
                # The name is re-stamped from the clip table, so a DCC rename cannot leave
                # the entry labelled with a clip that is gone.
                (NAME_KEY, clip_names[setting.index]),
                (ROOT_MOTION_KEY, setting.root_motion),
            ]
            if setting.root_bone:
                items.append((ROOT_BONE_KEY, setting.root_bone))
            return canonical_toml.InlineTable(items)

        domain[CLIPS_KEY] = [_entry(merged[index]) for index in sorted(merged)]
    elif isinstance(domain, dict):
        domain.pop(CLIPS_KEY, None)

    if isinstance(domain, dict):
        # The engine writes the domain's nested tables inline (`optimize = { … }`); a plain
        # dict here would come back as a `[glb.optimize]` header. Both parse the same, but the
        # inline spelling is what the domain's own writer produces, so flat members keep it.
        for key, value in domain.items():
            if isinstance(value, dict) and not isinstance(
                value, canonical_toml.InlineTable
            ) and not any(
                isinstance(member, dict)
                or (isinstance(member, list) and any(isinstance(e, dict) for e in member))
                for member in value.values()
            ):
                domain[key] = canonical_toml.InlineTable(value)
        if not domain:
            root.pop(DOMAIN)

    # A toggle that lands on the state already written -- or a reconcile that found nothing
    # to fix -- must not dirty the sidecar's stamp, or the watcher reconciles a file that
    # only looks changed.
    written = canonical_toml.dumps(root)
    if written != original:
        atomic.write_text(meta_path, written)
    _META_CACHE.pop(meta_path, None)


def _stored_entries(root: dict | None):
    """The ``[glb].clips`` array of a parsed sidecar; ``[]`` for anything that is not one."""
    if not isinstance(root, dict):
        return []
    domain = root.get(DOMAIN)
    if not isinstance(domain, dict):
        return []
    entries = domain.get(CLIPS_KEY)
    return entries if isinstance(entries, list) else []


def _parse_entries(entries) -> dict[int, ClipSetting]:
    """The stored ``clips`` array keyed by clip index; malformed entries are skipped, and a
    duplicated index keeps the last, the same ``BySlot`` rule the engine's reader uses."""
    parsed: dict[int, ClipSetting] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        index = entry.get(INDEX_KEY)
        if type(index) is not int or index < 0:
            continue
        parsed[index] = ClipSetting(
            index,
            entry.get(NAME_KEY) if isinstance(entry.get(NAME_KEY), str) else "",
            entry.get(ROOT_MOTION_KEY) is True,
            entry.get(ROOT_BONE_KEY) if isinstance(entry.get(ROOT_BONE_KEY), str) else "",
        )
    return parsed


def _collect(
    entries, clip_names: tuple[str, ...]
) -> tuple[dict[int, ClipSetting], list[ClipSetting]]:
    """Stored entries keyed by clip index, plus the ones the clip table no longer has.

    The index is the key the engine reads and the GLB's draw slots bind by, so it leads; the
    recorded name is the witness that catches the two ways a re-export can drift it:

    - a clip that MOVED (a sibling was removed, or the table reordered) has its name at
      another index, so the setting is re-keyed to follow it -- but only when the name lands
      on exactly one clip, since glTF names are not unique;
    - a clip RENAMED in place leaves the name nowhere, so the index match stands and the
      panel reports the drift until a write re-stamps the name;
    - an index past the end of the table is an orphan: the clip is gone, so the entry is
      reported and dropped from the next write rather than inherited by whatever slid into
      the slot.
    """
    merged: dict[int, ClipSetting] = {}
    orphans: list[ClipSetting] = []
    for setting in _parse_entries(entries).values():
        index = setting.index
        in_range = index < len(clip_names)
        if in_range and (not setting.name or clip_names[index] == setting.name):
            merged[index] = setting
            continue

        found = [i for i, name in enumerate(clip_names) if name and name == setting.name]
        if setting.name and len(found) == 1:
            merged[found[0]] = ClipSetting(
                found[0], setting.name, setting.root_motion, setting.root_bone)
        elif in_range:
            merged[index] = setting
        else:
            orphans.append(setting)
    return merged, orphans


def _read_meta(path: str) -> tuple[str, dict] | None:
    """``(text, model)`` of the sidecar, or ``None`` when it is missing or does not parse.

    The model comes through ``canonical_toml.loads`` so a write puts array elements back as
    the inline tables they were -- ``references`` and ``clips`` both live there.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    try:
        return text, canonical_toml.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return None


def _read_meta_cached(path: str) -> dict | None:
    """The sidecar's model behind a stamp cache: the panel asks at redraw rate."""
    stamp = _stamp(path)
    cached = _META_CACHE.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    loaded = _read_meta(path)
    found = loaded[1] if loaded is not None else None
    _META_CACHE[path] = (stamp, found)
    return found


def _stamp(path: str) -> str:
    try:
        info = os.stat(path)
    except OSError:
        return ""
    return f"{info.st_mtime_ns}:{info.st_size}"
