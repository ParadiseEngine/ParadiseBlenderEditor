"""The files a model can come from, and the GLB Blender reads for each.

A ``.glb`` is read as it is. A ``.blend`` or ``.fbx`` is converted to a GLB by the engine's
pipeline -- headless Blender, fixed export settings -- and the pipeline extracts meshes, clips
and materials from THAT GLB. So the viewport imports the very same file, at
``<root>/.editor/converted/<assets-relative source>.glb``, rather than opening the source
itself: anything Blender's own importer did differently from the converter would be a preview
of a model the game never gets.

The converted GLB records the SHA-256 of the source bytes it was made from
(``asset.extras.paradiseSourceSha256``). The pipeline refreshes it whenever it reads the source;
this module only answers whether the file on disk is current. Running the conversion is the
CLI's (``paradise assets convert``) -- see ``materialize/meshes.glb_of``.

Imports no ``bpy``.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable

from . import gltf, project

__all__ = [
    "CONVERTED",
    "SUFFIXES",
    "ConversionError",
    "convert_arguments",
    "converted_path",
    "current_glb",
    "edit_in_place_refusal",
    "is_converted",
    "is_current",
    "is_model",
    "printed_path",
]

#: Every extension a model source may have. ``.gltf`` is not one: the pipeline refuses it.
SUFFIXES = (".glb", ".blend", ".fbx")

#: The sources the pipeline converts to a GLB before reading them.
CONVERTED = (".blend", ".fbx")

#: Under the project's ``.editor/``: derived data, rebuilt on demand.
CONVERTED_DIR = "converted"

#: The ``asset.extras`` key naming the source bytes a converted GLB was made from.
SOURCE_SHA256_EXTRA = "paradiseSourceSha256"


class ConversionError(Exception):
    """A ``.blend``/``.fbx`` source has no current GLB and could not be given one."""


def is_model(path: str) -> bool:
    return path.lower().endswith(SUFFIXES)


def is_converted(path: str) -> bool:
    return path.lower().endswith(CONVERTED)


def converted_path(layout: project.ProjectLayout, source: str) -> str:
    """Where the pipeline keeps the GLB converted from ``source`` (absolute, under ``assets/``):
    ``assets/models/car.blend`` -> ``.editor/converted/models/car.blend.glb``."""
    relative = os.path.relpath(os.path.abspath(source), layout.assets)
    return os.path.join(layout.editor, CONVERTED_DIR, relative + ".glb")


def is_current(source: str, glb: str) -> bool:
    """Whether ``glb`` was converted from ``source`` as its bytes are now."""
    recorded = _stamped(glb, _recorded_sha256)
    return recorded is not None and recorded == _stamped(source, _sha256)


def current_glb(source: str) -> str | None:
    """The GLB to read for the model ``source``, without converting anything: the file itself
    for a ``.glb``, the converted GLB for a ``.blend``/``.fbx`` while it is current, else
    ``None``. For readers that must not start a process (a panel draw)."""
    if not is_converted(source):
        return source if os.path.isfile(source) else None
    layout = project.locate(source)
    if layout is None:
        return None
    glb = converted_path(layout, source)
    return glb if is_current(source, glb) else None


def convert_arguments(layout: project.ProjectLayout, source: str) -> list[str]:
    """The CLI verb that brings ``source``'s converted GLB up to date and prints its path."""
    return ["assets", "convert", os.path.abspath(source), "--project", layout.root]


def printed_path(stdout: str) -> str | None:
    """The GLB path ``paradise assets convert`` printed: its last non-empty stdout line."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    return lines[-1] if lines else None


def edit_in_place_refusal(source: str) -> str:
    """Why the converted model ``source`` is not edited by splicing its GLB, and what to do
    instead. The GLB is derived: the next conversion would overwrite an edit made there."""
    name = os.path.basename(source)
    if source.lower().endswith(".blend"):
        return (f"{name} is a .blend, and its GLB is converted from it: edit the .blend itself "
                "(Edit Source in New Blender) -- it keeps the quads and modifiers a GLB cannot -- "
                "and every placement follows once it is saved.")
    return (f"{name} is an FBX, an interchange file: edit the model in the application it came "
            "from and export the FBX again; every placement follows.")


#: path -> (mtime_ns, size, answer). A panel asks at redraw rate; hashing a large ``.blend``
#: each time is what the stamp saves.
_CACHE: dict[tuple[str, str], tuple[int, int, str | None]] = {}


def _stamped(path: str, compute: Callable[[str], str | None]) -> str | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = (compute.__name__, os.path.normcase(os.path.abspath(path)))
    cached = _CACHE.get(key)
    if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
        return cached[2]
    answer = compute(path)
    _CACHE[key] = (stat.st_mtime_ns, stat.st_size, answer)
    return answer


def _sha256(path: str) -> str | None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _recorded_sha256(glb: str) -> str | None:
    asset = gltf.read_json(glb).get("asset")
    extras = asset.get("extras") if isinstance(asset, dict) else None
    value = extras.get(SOURCE_SHA256_EXTRA) if isinstance(extras, dict) else None
    return value.lower() if isinstance(value, str) and value else None
