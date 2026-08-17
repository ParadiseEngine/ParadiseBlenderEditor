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

__all__ = [
    "Transcoder",
    "convert_data_directory",
    "convert_image",
    "encode_command",
    "encode_signature",
    "resolve_transcoder",
    "transcoder_version",
]

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

#: The subset of :data:`LINEAR_TOKENS` marking a TANGENT-SPACE NORMAL map specifically, which
#: needs `--normal-mode` on top of the linear format. That flag switches the UASTC encoder to
#: the two-channel "RRRG" layout (X in RGB, Y in alpha), and the runtime's transcoder ASSUMES
#: that layout: it targets BC5-RG, and its RGBA32 fallback runs an explicit G <- alpha swizzle
#: (`Ktx2Transcoder.SwizzleTwoChannelNormals`). Encode a normal map as plain UASTC RGB and the
#: two sides disagree about which channel holds Y -- the shader reconstructs Z from a bogus XY,
#: every shading normal tilts somewhere unrelated to the surface, and the model goes DARK
#: rather than obviously broken, because N.L collapses toward the ambient term.
NORMAL_TOKENS = frozenset({"normal", "normals", "nrm", "bump"})

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

    # PATH is not enough: a Blender launched from the Dock/Finder gets the GUI environment,
    # which omits /usr/local/bin and /opt/homebrew/bin — so the same scene converts fine from
    # a terminal-launched Blender and silently ships PNG from a GUI one. Probe the standard
    # install locations directly before giving up.
    for candidate in _WELL_KNOWN_LOCATIONS:
        if os.path.exists(candidate):
            return Transcoder(candidate, modern=_is_modern(candidate))

    return None


#: Standard install locations for KTX-Software's CLI, for hosts whose PATH does not carry
#: them (GUI-launched Blender on macOS above all).
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
    """Transcode one image to a ``.ktx2`` sidecar. Returns True if it ran.

    Skips when the sidecar is newer than its source, so re-exporting an unchanged project does
    not re-encode every texture (transcoding a large sheet takes seconds, and an author saves
    often).
    """
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
    """The exact argv that encodes ``source_path`` -- the single source of truth for HOW.

    Split out from :func:`convert_image` because it is also what :func:`encode_signature` hashes:
    an encode is identified by its command line, so adding or changing a flag here invalidates
    every cached artifact by construction, with no version constant to remember to bump.
    """
    if transcoder.modern:
        # `--format` is required, and picks the transfer function: colour is sRGB, data is not.
        # Note the argument order -- input BEFORE output, the reverse of toktx.
        #
        # `--assign-tf` is NOT optional decoration: an 8-bit PNG carries no transfer-function
        # tag, and `ktx create` then ASSUMES sRGB and silently applies a "visual lossy color
        # conversion" (its own words -- the warning lands in captured stderr nobody reads) to
        # match a linear --format. A flat normal-map texel 128 comes out ~55, i.e. the whole
        # map gains a constant negative X/Y bias and every shading normal tilts away from the
        # light: the model renders uniformly DARK, not visibly broken. Roughness/ORM maps get
        # the same silent darkening. --assign-tf pins the interpretation, pixels pass through.
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
        # Normal maps additionally need the two-channel encoder mode the runtime expects --
        # see :data:`NORMAL_TOKENS`. Linear alone is not enough.
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
    """Everything that decides an encode's OUTPUT bytes, as one string, for cache keys.

    That is the argv with both file paths replaced by placeholders -- the paths themselves are
    irrelevant to the result, while the flags derived from the file NAME (sRGB vs linear vs
    ``--normal-mode``, see :data:`LINEAR_TOKENS`) are decisive -- plus the transcoder's version.

    Keying on the name-derived flags rather than the name is what makes the cache safe to share
    between images: two files with identical pixels but names like ``rock_BaseColor`` and
    ``rock_Normal`` encode differently and must never resolve to one entry.
    """
    command = encode_command(source_path, "<target>", transcoder)
    rendered = " ".join(
        "<source>" if argument == source_path else argument for argument in command[1:]
    )
    return f"{os.path.basename(transcoder.path)} {rendered} {transcoder_version(transcoder)}"


#: Memoized ``--version`` output per binary path. A KTX-Software upgrade changes encoder output,
#: so it belongs in the key; running the probe once per session keeps it free.
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
        # No version to read: fall back to the binary's own identity. Coarser, but it still
        # changes when the tool is replaced, which is the case that matters.
        try:
            stat = os.stat(transcoder.path)
            version = f"size={stat.st_size} mtime={int(stat.st_mtime)}"
        except OSError:
            version = "unknown"

    _versions[transcoder.path] = version
    return version


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
