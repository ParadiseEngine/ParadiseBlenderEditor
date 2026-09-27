"""The files a model can come from, and the GLB Blender reads for each.

A ``.glb`` or a ``.gltf`` (:data:`DIRECT`) is read as it is -- a ``.gltf`` is the same asset as a
GLB whose buffers live beside it (``document/gltf.py``). Every other model format
(:data:`CONVERTED`) is converted to a GLB by the engine's pipeline -- headless Blender, fixed
import and export settings -- and the pipeline extracts meshes, clips and materials from THAT GLB.
So the viewport imports the very same file, at
``<root>/.editor/converted/<assets-relative source>.glb``, rather than opening the source
itself: anything Blender's own importer did differently from the converter would be a preview
of a model the game never gets.

The converted GLB records the SHA-256 of the source bytes it was made from
(``asset.extras.paradiseSourceSha256``) and of every external file Blender loaded while
importing it -- textures, an ``.obj``'s ``.mtl``, linked libraries (``paradiseDependencies``,
paths relative to the source's directory). The pipeline refreshes it whenever it reads the
source; this module only answers whether the file on disk is current. Running the conversion is
the CLI's (``paradise assets convert``) -- see ``materialize/meshes.glb_of``.

A ``.blend`` whose collections are marked as assets holds one model per asset collection, named
by the collection (:class:`Model`'s ``asset``). Each is converted to a GLB of its own,
``<root>/.editor/converted/<assets-relative source>/<asset>.glb``, carrying the same stamp; a
``.blend`` with no asset collection is one model, converted as a whole.

Imports no ``bpy``.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass

from . import gltf, project

__all__ = [
    "CONVERTED",
    "DIRECT",
    "SUFFIXES",
    "ConversionError",
    "Model",
    "convert_arguments",
    "converted_path",
    "current_glb",
    "edit_in_place_refusal",
    "is_asset_name",
    "is_converted",
    "is_current",
    "is_model",
    "is_skeleton_only",
    "no_mesh_refusal",
    "printed_path",
]

#: The sources read as they are: glTF, binary or JSON with its buffers beside it.
DIRECT = (".glb", ".gltf")

#: The sources the pipeline converts to a GLB before reading them: every other format headless
#: Blender imports. A ``.bvh`` carries animation only. Collada left Blender in 5.0 and is not one.
CONVERTED = (
    ".blend", ".fbx", ".obj", ".ply", ".stl",
    ".usd", ".usda", ".usdc", ".usdz", ".abc", ".bvh",
)

#: Every extension a model source may have.
SUFFIXES = (*DIRECT, *CONVERTED)

#: Under the project's ``.editor/``: derived data, rebuilt on demand.
CONVERTED_DIR = "converted"

#: The ``asset.extras`` key naming the source bytes a converted GLB was made from.
SOURCE_SHA256_EXTRA = "paradiseSourceSha256"

#: The ``asset.extras`` key listing ``{path, sha256}`` for each file the import read besides
#: the source. Always written, possibly empty; a GLB without it predates dependency tracking.
DEPENDENCIES_EXTRA = "paradiseDependencies"

#: The ``asset.extras`` key naming the asset collection a per-asset GLB was converted from;
#: absent on a whole-file conversion.
ASSET_EXTRA = "paradiseAsset"


class ConversionError(Exception):
    """A converted source has no current GLB and could not be given one."""


@dataclass(frozen=True)
class Model:
    """One model: the source file (absolute), and the asset in it when the source is a
    ``.blend`` holding several. ``asset = None`` is the whole file."""

    path: str
    asset: str | None = None

    @property
    def label(self) -> str:
        """How a message names the model: ``Crate.blend``, or ``Lamp_A in Lamps.blend``."""
        name = os.path.basename(self.path)
        return name if self.asset is None else f"{self.asset} in {name}"


def is_asset_name(name: object) -> bool:
    """Whether ``name`` can name an asset: a file stem, since it names the asset's converted GLB
    and the documents extracted from it -- never a path that would land elsewhere."""
    return (isinstance(name, str) and bool(name) and name not in (".", "..")
            and not any(separator in name for separator in ("/", "\\", "\0")))


def is_model(path: str) -> bool:
    return path.lower().endswith(SUFFIXES)


def is_converted(path: str) -> bool:
    return path.lower().endswith(CONVERTED)


def converted_path(layout: project.ProjectLayout, source: str, asset: str | None = None) -> str:
    """Where the pipeline keeps the GLB converted from ``source`` (absolute, under ``assets/``):
    ``assets/models/car.blend`` -> ``.editor/converted/models/car.blend.glb``, and its asset
    ``Van`` -> ``.editor/converted/models/car.blend/Van.glb``."""
    relative = os.path.relpath(os.path.abspath(source), layout.assets)
    if asset is None:
        return os.path.join(layout.editor, CONVERTED_DIR, relative + ".glb")
    return os.path.join(layout.editor, CONVERTED_DIR, relative, asset + ".glb")


def is_current(source: str, glb: str, asset: str | None = None) -> bool:
    """Whether ``glb`` was converted from ``source`` -- from its ``asset``, or from the whole file
    -- and its dependencies as their bytes are now. A dependency that is gone makes it stale, like
    one that changed."""
    recorded = _stamped(glb, _recorded_stamp)
    if recorded is None or recorded[0] != _stamped(source, _sha256) or recorded[2] != asset:
        return False
    directory = os.path.dirname(os.path.abspath(source))
    return all(
        _stamped(os.path.normpath(os.path.join(directory, relative)), _sha256) == sha256
        for relative, sha256 in recorded[1])


def is_skeleton_only(source: str, asset: str | None = None) -> bool:
    """Whether the current GLB of ``source`` (or of its ``asset``) holds no mesh -- a ``.bvh``,
    or any file of skeleton and clips alone. ``False`` while there is no current GLB to ask:
    nothing is known yet."""
    glb = current_glb(source, asset)
    return glb is not None and _stamped(glb, _has_no_mesh) is True


def current_glb(source: str, asset: str | None = None) -> str | None:
    """The GLB to read for the model ``source`` (or its ``asset``), without converting anything:
    the file itself for a ``.glb`` or ``.gltf``, the converted GLB for any other source while it
    is current, else ``None``. For readers that must not start a process (a panel draw).

    An asset of a ``.glb`` or ``.gltf`` is ``None`` too: only a converted source holds several
    models, so the reference names nothing the pipeline makes."""
    if not is_converted(source):
        return source if asset is None and os.path.isfile(source) else None
    layout = project.locate(source)
    if layout is None or (asset is not None and not is_asset_name(asset)):
        return None
    glb = converted_path(layout, source, asset)
    return glb if is_current(source, glb, asset) else None


def convert_arguments(layout: project.ProjectLayout, source: str, asset: str | None = None) -> list[str]:
    """The CLI verb that brings the converted GLB of ``source`` (or of its ``asset``) up to date
    and prints its path last. One run converts every asset of the file either way."""
    arguments = ["assets", "convert", os.path.abspath(source), "--project", layout.root]
    return arguments if asset is None else [*arguments, "--asset", asset]


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
    label = _format_label(source)
    return (f"{name} is {_article(label)} {label} file, an interchange format: edit the model in "
            f"the application it came from and export the {label} again; every placement follows.")


def no_mesh_refusal(source: str, asset: str | None = None) -> str:
    """Why a model whose GLB holds no mesh -- a ``.bvh``, or any file of skeleton and clips
    alone -- has no geometry to edit, and where its clips are authored instead."""
    return (f"{Model(source, asset).label} holds no mesh, only a skeleton and its animation: there "
            "is no geometry to edit. Its clips are set up in the Animation Clips section of the "
            "Components panel.")


def _format_label(source: str) -> str:
    """How a message names ``source``'s format: ``OBJ``, ``USDZ``."""
    return os.path.splitext(source)[1][1:].upper()


def _article(label: str) -> str:
    # By the spelled-out first letter: "an FBX", "an OBJ", "a PLY", "a USD".
    return "an" if label[:1] and label[:1] in "AEFHILMNORSX" else "a"


#: path -> (mtime_ns, size, answer). A panel asks at redraw rate; hashing a large ``.blend``
#: each time is what the stamp saves.
_CACHE: dict[tuple[str, str], tuple[int, int, object]] = {}


def _stamped[T](path: str, compute: Callable[[str], T]) -> T | None:
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


def _has_no_mesh(glb: str) -> bool:
    document = gltf.read_json(glb)
    return bool(document) and not document.get("meshes")


def _recorded_stamp(glb: str) -> tuple[str, tuple[tuple[str, str], ...], str | None] | None:
    """The source SHA-256, the ``(relative path, sha256)`` dependencies and the asset ``glb``
    records, or ``None`` when the first two are missing or malformed: such a GLB is never
    current."""
    asset = gltf.read_json(glb).get("asset")
    extras = asset.get("extras") if isinstance(asset, dict) else None
    if not isinstance(extras, dict):
        return None
    source = extras.get(SOURCE_SHA256_EXTRA)
    listed = extras.get(DEPENDENCIES_EXTRA)
    if not isinstance(source, str) or not source or not isinstance(listed, list):
        return None
    dependencies = []
    for entry in listed:
        path = entry.get("path") if isinstance(entry, dict) else None
        sha256 = entry.get("sha256") if isinstance(entry, dict) else None
        if not isinstance(path, str) or not path or not isinstance(sha256, str) or not sha256:
            return None
        dependencies.append((path, sha256.lower()))
    asset = extras.get(ASSET_EXTRA)
    return source.lower(), tuple(dependencies), asset if isinstance(asset, str) else None
