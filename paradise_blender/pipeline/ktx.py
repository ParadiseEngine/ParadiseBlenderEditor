"""KTX2 texture transcoding.

**The engine's glTF reader rejects PNG and JPEG.** Textures must be KTX2, so a scene whose
images were never transcoded exports cleanly and then renders untextured -- a failure that
looks like a material bug rather than a missing pipeline step. That is why this runs as part
of the export flow and why its absence is warned about loudly.

The transcoder is KTX-Software's ``toktx``, the same tool the Godot host drives. It is
optional at install time: without it exports still succeed, textures just stay unconverted.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from .. import log
from ..paths import ExportPaths

__all__ = ["convert_data_directory", "convert_image", "resolve_toktx"]

#: Source formats worth transcoding. Anything else is passed over silently.
SOURCE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tga")

_TIMEOUT_SECONDS = 300


def resolve_toktx() -> str | None:
    """Path to ``toktx``: the configured one, else PATH."""
    from ..prefs import get_preferences

    try:
        configured = get_preferences().ktx_path.strip()
    except (KeyError, AttributeError):
        configured = ""

    if configured:
        expanded = os.path.expanduser(configured)
        if os.path.exists(expanded):
            return expanded

    return shutil.which("toktx")


def convert_image(source_path: str, toktx: str, force: bool = False) -> bool:
    """Transcode one image to a ``.ktx2`` sidecar. Returns True if it ran.

    Skips when the sidecar is newer than its source, so re-exporting an unchanged project does
    not re-encode every texture (transcoding a large sheet takes seconds, and an author saves
    often).
    """
    target = os.path.splitext(source_path)[0] + ".ktx2"

    if not force and os.path.exists(target) and os.path.getmtime(target) >= os.path.getmtime(source_path):
        return False

    try:
        result = subprocess.run(
            [
                toktx,
                "--t2",              # KTX2 container
                "--encode", "uastc",  # high-quality transcodable format
                "--genmipmap",
                target,
                source_path,
            ],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        log.warn(f"toktx failed on '{source_path}': {error}")
        return False

    if result.returncode != 0:
        log.warn(f"toktx failed on '{source_path}': {result.stderr.strip()}")
        return False

    return True


def convert_data_directory(paths: ExportPaths, force: bool = False) -> tuple[int, int]:
    """Transcode every convertible image under the data directory.

    Returns ``(converted, skipped)``. A missing ``toktx`` returns ``(0, 0)`` after warning --
    the caller reports it, and the export is still valid, just untextured at runtime.
    """
    toktx = resolve_toktx()
    if toktx is None:
        log.warn(
            "toktx not found, so textures were not transcoded to KTX2. The engine's glTF "
            "reader rejects PNG/JPEG, so textured meshes will render untextured. Install "
            "KTX-Software and set its path in the addon preferences."
        )
        return (0, 0)

    converted = 0
    skipped = 0

    for root, _dirs, files in os.walk(paths.data_dir):
        for name in files:
            if not name.lower().endswith(SOURCE_EXTENSIONS):
                continue
            if convert_image(os.path.join(root, name), toktx, force):
                converted += 1
            else:
                skipped += 1

    return (converted, skipped)
