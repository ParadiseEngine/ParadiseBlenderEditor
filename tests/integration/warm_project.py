"""A temp copy of an asset project whose build outputs carry over from the last passing run.

The integration tests copy ``assets/`` into an empty project, so without this every run
rebuilds all of ShiningPie from nothing: ~12 minutes for ``assets build`` and ~14 for the
watcher's ``--editor`` build into ``.editor/play``. The build index skips an input whose mtime
and size are unchanged, so the same outputs, copied back next to an mtime-preserving copy of
``assets/``, make both builds incremental (~10 s). The checkout's own ``build/`` is no substitute:
its index was written by whatever CLI built it, and a different pipeline version rebuilds all.

Outputs are stored per source project under ``PARADISE_TEST_WARM_DIR`` (default
``~/.cache/paradise-assets-tests``) and only after a run passes, so a failing run cannot seed the
next one. ``PARADISE_TEST_WARM_DIR=off`` disables it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

#: What is carried over, relative to the project root. The artifact cache holds encoded textures.
_OUTPUTS = ("build", ".editor/play", ".editor/cache")
_DISABLED = {"0", "off", "false", "no", "none"}


def copy_project(source: str | os.PathLike, root: str | os.PathLike) -> None:
    """``assets/`` and the schema dump of ``source`` into ``root``, plus the warm outputs."""
    source, root = Path(source), Path(root)
    # copytree copies with copy2, which keeps mtimes: the build index's cheap tier depends on it.
    shutil.copytree(source / "assets", root / "assets")
    (root / ".editor").mkdir(exist_ok=True)
    shutil.copy2(source / ".editor/authoring-schema.json", root / ".editor/authoring-schema.json")
    slot = _slot(source)
    if slot is None:
        return
    for relative in _OUTPUTS:
        if (slot / relative).is_dir():
            shutil.copytree(slot / relative, root / relative, dirs_exist_ok=True)


def keep_warm(source: str | os.PathLike, root: str | os.PathLike) -> None:
    """Store ``root``'s outputs as the warm start for the next run against ``source``."""
    slot = _slot(Path(source))
    if slot is None:
        return
    staging = slot.with_name(slot.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    for relative in _OUTPUTS:
        if (Path(root) / relative).is_dir():
            shutil.copytree(Path(root) / relative, staging / relative)
    # Replaced whole, so an interrupted store leaves the previous warm start, not half of one.
    shutil.rmtree(slot, ignore_errors=True)
    staging.rename(slot)


def _slot(source: Path) -> Path | None:
    configured = os.environ.get("PARADISE_TEST_WARM_DIR", "").strip()
    if configured.lower() in _DISABLED:
        return None
    base = Path(configured).expanduser() if configured else Path.home() / ".cache/paradise-assets-tests"
    key = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:16]
    base.mkdir(parents=True, exist_ok=True)
    return base / key
