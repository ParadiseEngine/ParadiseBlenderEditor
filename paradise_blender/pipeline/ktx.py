"""KTX2 texture transcoding.

**The engine's glTF reader rejects PNG and JPEG.** Textures must be KTX2, so a scene whose
images were never transcoded exports cleanly and then renders untextured -- a failure that
looks like a material bug rather than a missing pipeline step. That is why this runs as part
of the export flow and why its absence is warned about loudly.

The transcoder is KTX-Software's CLI, which comes in two incompatible flavours and this module
drives whichever is installed:

* ``ktx create`` -- KTX-Software 4.4 and newer, where ``toktx`` was removed. Arguments run
  ``input output``, and ``--format`` is mandatory.
* ``toktx`` -- the legacy tool, still what the Godot host drives on older machines. Arguments
  run ``output input``, the reverse.

Getting this wrong is silent rather than loud: passing toktx's argument order to ``ktx create``
makes it try to read the destination as an input image.

Either is optional at install time -- without one, exports still succeed and textures stay
unconverted.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import NamedTuple

from .. import log
from ..paths import ExportPaths

__all__ = ["Transcoder", "convert_data_directory", "convert_image", "resolve_transcoder"]

#: Source formats worth transcoding. Anything else is passed over silently.
SOURCE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tga")

#: Filename tokens marking a texture whose values are DATA, not colour. Encoding a normal or
#: roughness map as sRGB bakes the transfer function into the numbers and lights the model
#: wrongly -- subtly enough to read as a shader bug. Matched per underscore-separated token, so
#: `T_Superhero_Male_Normal.png` and `hero_normal_map.png` both resolve to linear.
LINEAR_TOKENS = frozenset(
    {
        "normal",
        "normals",
        "nrm",
        "roughness",
        "rough",
        "metallic",
        "metalness",
        "orm",
        "arm",
        "ao",
        "occlusion",
        "height",
        "displacement",
        "disp",
        "bump",
        "mask",
        "specular",
        "opacity",
        "emissivemask",
        "data",
        "linear",
    }
)

_TIMEOUT_SECONDS = 300


class Transcoder(NamedTuple):
    """A resolved KTX-Software CLI and which argument dialect it speaks."""

    path: str
    #: True for modern ``ktx create``; False for legacy ``toktx``.
    modern: bool

    @property
    def name(self) -> str:
        return "ktx create" if self.modern else "toktx"


def resolve_transcoder() -> Transcoder | None:
    """The KTX-Software CLI to drive, or ``None`` when neither is installed.

    Order: the configured path, then ``ktx`` on PATH, then ``toktx``. The configured path may
    point at either tool, so the dialect is decided by its filename rather than assumed.
    """
    from ..prefs import get_preferences

    try:
        configured = get_preferences().ktx_path.strip()
    except (KeyError, AttributeError):
        configured = ""

    if configured:
        expanded = os.path.expanduser(configured)
        if os.path.exists(expanded):
            return Transcoder(expanded, modern=_is_modern(expanded))
        log.warn(f"Configured toktx/ktx path '{configured}' does not exist; auto-detecting.")

    # `ktx` first: on a machine with both, it is the newer install.
    for candidate in ("ktx", "toktx"):
        found = shutil.which(candidate)
        if found:
            return Transcoder(found, modern=_is_modern(found))

    return None


def _is_modern(path: str) -> bool:
    """Legacy iff the binary is literally named ``toktx``."""
    return "toktx" not in os.path.basename(path).lower()


def is_linear(source_path: str) -> bool:
    """Whether this texture holds data rather than colour -- see :data:`LINEAR_TOKENS`."""
    stem = os.path.splitext(os.path.basename(source_path))[0].lower()
    return any(token in LINEAR_TOKENS for token in stem.replace("-", "_").split("_"))


def convert_image(source_path: str, transcoder: Transcoder, force: bool = False) -> bool:
    """Transcode one image to a ``.ktx2`` sidecar. Returns True if it ran.

    Skips when the sidecar is newer than its source, so re-exporting an unchanged project does
    not re-encode every texture (transcoding a large sheet takes seconds, and an author saves
    often).
    """
    target = os.path.splitext(source_path)[0] + ".ktx2"

    if not force and os.path.exists(target) and os.path.getmtime(target) >= os.path.getmtime(source_path):
        return False

    if transcoder.modern:
        # `--format` is required, and picks the transfer function: colour is sRGB, data is not.
        # Note the argument order -- input BEFORE output, the reverse of toktx.
        command = [
            transcoder.path,
            "create",
            "--format",
            "R8G8B8A8_UNORM" if is_linear(source_path) else "R8G8B8A8_SRGB",
            "--encode",
            "uastc",
            "--generate-mipmap",
            source_path,
            target,
        ]
    else:
        command = [
            transcoder.path,
            "--t2",  # KTX2 container
            "--encode",
            "uastc",  # high-quality transcodable format
            "--genmipmap",
            target,
            source_path,
        ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        log.warn(f"{transcoder.name} failed on '{source_path}': {error}")
        return False

    if result.returncode != 0:
        log.warn(f"{transcoder.name} failed on '{source_path}': {result.stderr.strip()}")
        return False

    return True


def convert_data_directory(paths: ExportPaths, force: bool = False) -> tuple[int, int]:
    """Transcode every convertible image under the data directory.

    Returns ``(converted, skipped)``. A missing transcoder returns ``(0, 0)`` after warning --
    the caller reports it, and the export is still valid, just untextured at runtime.
    """
    transcoder = resolve_transcoder()
    if transcoder is None:
        log.warn(
            "Neither `ktx` nor `toktx` was found, so textures were not transcoded to KTX2. The "
            "engine's glTF reader rejects PNG/JPEG, so textured meshes will render untextured. "
            "Install KTX-Software and set its path in the addon preferences."
        )
        return (0, 0)

    converted = 0
    skipped = 0

    for root, _dirs, files in os.walk(paths.data_dir):
        for name in files:
            if not name.lower().endswith(SOURCE_EXTENSIONS):
                continue
            if convert_image(os.path.join(root, name), transcoder, force):
                converted += 1
            else:
                skipped += 1

    return (converted, skipped)
