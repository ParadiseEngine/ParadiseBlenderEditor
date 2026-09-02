"""KTX2 texture transcoding. The engine's glTF reader rejects PNG/JPEG, and an untranscoded
scene renders untextured looking like a material bug, so a missing transcoder warns loudly.

Two incompatible CLIs are driven: ``ktx create`` (4.4+, ``input output``, ``--format``
mandatory) and legacy ``toktx`` (``output input``). Passing toktx's order to ``ktx create`` makes
it silently read the destination as an input image.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import NamedTuple

from .. import log
from ..paths import ExportPaths

__all__ = [
    "Transcoder",
    "convert_data_directory",
    "convert_image",
    "encode_command",
    "encode_signature",
    "resolve_transcoder",
    "transcoder_version",
]

SOURCE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tga")

#: Filename tokens marking DATA textures. Encoding a normal or roughness map as sRGB bakes the
#: transfer function into the numbers and reads as a shader bug. Matched per token.
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

#: Tangent-space normal maps need `--normal-mode` (RRRG: X in RGB, Y in alpha), which the
#: runtime's transcoder ASSUMES (BC5-RG, or a G <- alpha swizzle). Plain UASTC RGB makes the
#: shader reconstruct Z from a bogus XY and the model goes DARK rather than visibly broken.
NORMAL_TOKENS = frozenset({"normal", "normals", "nrm", "bump"})

_TIMEOUT_SECONDS = 300


class Transcoder(NamedTuple):
    """A resolved KTX-Software CLI and which argument dialect it speaks."""

    path: str
    modern: bool

    @property
    def name(self) -> str:
        return "ktx create" if self.modern else "toktx"


def resolve_transcoder() -> Transcoder | None:
    """The KTX-Software CLI, or ``None``; the dialect comes from the filename, not assumed."""
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

    # A Dock-launched Blender's PATH omits /usr/local/bin and /opt/homebrew/bin, so a scene
    # that converts from a terminal silently ships PNG from the GUI.
    for candidate in _WELL_KNOWN_LOCATIONS:
        if os.path.exists(candidate):
            return Transcoder(candidate, modern=_is_modern(candidate))

    return None


#: Install locations a GUI-launched Blender's PATH does not carry.
_WELL_KNOWN_LOCATIONS = (
    "/opt/homebrew/bin/ktx",
    "/usr/local/bin/ktx",
    "/opt/homebrew/bin/toktx",
    "/usr/local/bin/toktx",
    "C:\\Program Files\\KTX-Software\\bin\\ktx.exe",
    "C:\\Program Files\\KTX-Software\\bin\\toktx.exe",
)


def _is_modern(path: str) -> bool:
    """Legacy iff the binary is literally named ``toktx``."""
    return "toktx" not in os.path.basename(path).lower()


def _tokens(source_path: str) -> list[str]:
    stem = os.path.splitext(os.path.basename(source_path))[0].lower()
    return stem.replace("-", "_").split("_")


def is_linear(source_path: str) -> bool:
    """Whether this texture holds data rather than colour -- see :data:`LINEAR_TOKENS`."""
    return any(token in LINEAR_TOKENS for token in _tokens(source_path))


def is_normal_map(source_path: str) -> bool:
    """Whether this texture is a tangent-space normal map -- see :data:`NORMAL_TOKENS`."""
    return any(token in NORMAL_TOKENS for token in _tokens(source_path))


def convert_image(source_path: str, transcoder: Transcoder, force: bool = False) -> bool:
    """Transcode one image to a ``.ktx2`` sidecar unless the sidecar is newer; True if it ran."""
    target = os.path.splitext(source_path)[0] + ".ktx2"

    if not force and os.path.exists(target) and os.path.getmtime(target) >= os.path.getmtime(source_path):
        return False

    command = encode_command(source_path, target, transcoder)

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


def encode_command(source_path: str, target_path: str, transcoder: Transcoder) -> list[str]:
    """The exact encode argv. Also what :func:`encode_signature` hashes, so a flag change here
    invalidates every cached artifact with no version constant to bump."""
    if transcoder.modern:
        # `--assign-tf` is NOT decoration: an 8-bit PNG carries no transfer-function tag, and
        # `ktx create` then assumes sRGB and silently converts to match a linear --format. A
        # flat normal texel 128 comes out ~55 and the model renders uniformly DARK.
        command = [
            transcoder.path,
            "create",
            "--format",
            "R8G8B8A8_UNORM" if is_linear(source_path) else "R8G8B8A8_SRGB",
            "--assign-tf",
            "linear" if is_linear(source_path) else "srgb",
            "--encode",
            "uastc",
            "--generate-mipmap",
        ]
        # Linear alone is not enough for a normal map; see NORMAL_TOKENS.
        if is_normal_map(source_path):
            command.append("--normal-mode")
        command += [
            source_path,
            target_path,
        ]
    else:
        command = [
            transcoder.path,
            "--t2",  # KTX2 container
            "--encode",
            "uastc",  # high-quality transcodable format
            "--genmipmap",
            target_path,
            source_path,
        ]

    return command


def encode_signature(source_path: str, transcoder: Transcoder) -> str:
    """Everything that decides an encode's output bytes: the argv with paths replaced by
    placeholders, plus the transcoder version. Keyed on the name-derived flags, not the name, so
    identical pixels named ``rock_BaseColor`` and ``rock_Normal`` never share an entry."""
    command = encode_command(source_path, "<target>", transcoder)
    rendered = " ".join(
        "<source>" if argument == source_path else argument for argument in command[1:]
    )
    return f"{os.path.basename(transcoder.path)} {rendered} {transcoder_version(transcoder)}"


#: Memoized ``--version`` per binary; an upgrade changes encoder output, so it is in the key.
_versions: dict[str, str] = {}


def transcoder_version(transcoder: Transcoder) -> str:
    """The transcoder's reported version, or a stat-based stand-in if it will not report one."""
    cached = _versions.get(transcoder.path)
    if cached is not None:
        return cached

    version = ""
    try:
        result = subprocess.run(
            [transcoder.path, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        version = (result.stdout or result.stderr).strip().splitlines()[0] if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired, IndexError):
        version = ""

    if not version:
        # Coarser, but it still changes when the tool is replaced.
        try:
            stat = os.stat(transcoder.path)
            version = f"size={stat.st_size} mtime={int(stat.st_mtime)}"
        except OSError:
            version = "unknown"

    _versions[transcoder.path] = version
    return version


def convert_data_directory(paths: ExportPaths, force: bool = False) -> tuple[int, int]:
    """Transcode every convertible image under ``data/``; ``(converted, skipped)``."""
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
