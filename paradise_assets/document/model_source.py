"""The files a model can come from, how a level shows each, and the GLB the engine reads for it.

A ``.glb`` or a ``.gltf`` (:data:`DIRECT`) is read as it is -- a ``.gltf`` is the same asset as a
GLB whose buffers live beside it (``document/gltf.py``). Every other model format
(:data:`CONVERTED`) is converted to a GLB by the engine's pipeline -- headless Blender, fixed
import and export settings -- and the pipeline extracts meshes, clips and materials from THAT GLB,
kept at ``<root>/.editor/converted/<assets-relative source>.glb``.

A placement in a level shows the SOURCE, loaded natively, so a level designer sees the model as
authored -- quads, live modifiers, its own materials and object hierarchy -- rather than the
triangulated export: a ``.blend`` is linked (``materialize/meshes.py``), any other source goes
through :func:`importer_for`, the very importer call the converter makes. The converted GLB is only
what the engine cooks and what reads the engine's structure: clip authoring and
:func:`is_skeleton_only`. A model's geometry is edited in its source, never here: a ``.blend`` in
a Blender of its own (Edit Source in New Blender), any other format in the application that
exported it (:func:`edit_source_refusal`).

The converted GLB records the SHA-256 of the source bytes it was made from
(``asset.extras.paradiseSourceSha256``) and of every external file Blender loaded while
importing it -- textures, an ``.obj``'s ``.mtl``, linked libraries (``paradiseDependencies``,
paths relative to the source's directory). The pipeline refreshes it whenever it reads the
source (the asset watcher, ``paradise assets convert``); this module only answers whether the
file on disk is current.

A ``.blend`` whose collections are marked as assets holds one model per asset collection,
identified by the GUID the collection carries in its ``paradise_guid`` custom property
(``document/asset_guids.py``) -- never by its name, so renaming the collection keeps every
identity. :class:`Model`'s ``asset`` is that GUID and its ``name`` the collection's name, a hint
for messages. Each asset is converted to a GLB of its own,
``<root>/.editor/converted/<assets-relative source>/<asset guid>.glb``, carrying the same stamp; a
``.blend`` with no asset collection is one model, converted as a whole.

Imports no ``bpy``.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field

from . import gltf, project
from . import guid as document_guid

__all__ = [
    "ANIMATION_ONLY",
    "CONVERTED",
    "DIRECT",
    "GLTF_IMPORTER",
    "IMPORTERS",
    "MESH_SUFFIXES",
    "SUFFIXES",
    "Importer",
    "Model",
    "converted_path",
    "current_glb",
    "edit_source_refusal",
    "importer_for",
    "is_converted",
    "is_current",
    "is_linked",
    "is_model",
    "is_skeleton_only",
    "native_dependencies",
    "no_mesh_refusal",
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


@dataclass(frozen=True)
class Importer:
    """A Blender import operator, ``<module>.<name>`` under ``bpy.ops``, and the options it is
    called with besides ``filepath``."""

    operator: str
    options: tuple[tuple[str, object], ...] = ()


#: The importer each converted source is read with, options included. Mirrors ``s_importers`` in
#: the engine's ``BlenderModelConverter`` (Paradise.Assets.Pipeline): a placement must show what
#: the converter itself imported, so a change there is a change here. A ``.blend`` has none -- the
#: converter opens it and the level links it.
IMPORTERS: dict[str, Importer] = {
    ".fbx": Importer("import_scene.fbx", (("automatic_bone_orientation", True),)),
    ".obj": Importer("wm.obj_import"),
    ".ply": Importer("wm.ply_import"),
    ".stl": Importer("wm.stl_import"),
    ".usd": Importer("wm.usd_import"),
    ".usda": Importer("wm.usd_import"),
    ".usdc": Importer("wm.usd_import"),
    ".usdz": Importer("wm.usd_import"),
    ".abc": Importer("wm.alembic_import"),
    ".bvh": Importer("import_anim.bvh"),
}

#: A ``.glb`` or ``.gltf`` is what the engine reads itself, so Blender's glTF importer shows it.
GLTF_IMPORTER = Importer("import_scene.gltf")

#: Formats that carry a skeleton and its animation, never a mesh.
ANIMATION_ONLY = (".bvh",)

#: Formats that can hold geometry: what a mesh slot's picker offers.
MESH_SUFFIXES = tuple(suffix for suffix in SUFFIXES if suffix not in ANIMATION_ONLY)

#: Under the project's ``.editor/``: derived data, rebuilt on demand.
CONVERTED_DIR = "converted"

#: The ``asset.extras`` key naming the source bytes a converted GLB was made from.
SOURCE_SHA256_EXTRA = "paradiseSourceSha256"

#: The ``asset.extras`` key listing ``{path, sha256}`` for each file the import read besides
#: the source. Always written, possibly empty; a GLB without it predates dependency tracking.
DEPENDENCIES_EXTRA = "paradiseDependencies"

#: The ``asset.extras`` key holding the GUID of the asset collection a per-asset GLB was
#: converted from; absent on a whole-file conversion.
ASSET_EXTRA = "paradiseAsset"


@dataclass(frozen=True)
class Model:
    """One model: the source file (absolute), and the asset in it when the source is a
    ``.blend`` holding several -- its canonical GUID, with ``name``, the asset collection's name
    as last recorded, for messages only. ``asset = None`` is the whole file."""

    path: str
    asset: str | None = None
    name: str | None = field(default=None, compare=False)

    @property
    def label(self) -> str:
        """How a message names the model: ``Crate.blend``, or ``Lamp_A in Lamps.blend``."""
        file = os.path.basename(self.path)
        return file if self.asset is None else f"{self.name or self.asset} in {file}"


def is_model(path: str) -> bool:
    return path.lower().endswith(SUFFIXES)


def is_converted(path: str) -> bool:
    return path.lower().endswith(CONVERTED)


def is_linked(path: str) -> bool:
    """Whether a placement of ``path`` links it rather than importing it: a ``.blend``, whose
    data a level shows read-only, as the file holds it."""
    return path.lower().endswith(".blend")


def importer_for(path: str) -> Importer | None:
    """The importer a placement of ``path`` is shown through; ``None`` for a ``.blend``
    (:func:`is_linked`) and for a file that is no model."""
    extension = os.path.splitext(path)[1].lower()
    return GLTF_IMPORTER if extension in DIRECT else IMPORTERS.get(extension)


def native_dependencies(path: str) -> list[str]:
    """The files besides ``path`` an import of it reads that no datablock it makes names -- a
    ``.gltf``'s buffers, an ``.obj``'s material libraries -- absolute. Images are named by the
    datablocks the import makes, so the caller reads them there."""
    if gltf.is_gltf(path):
        return gltf.buffer_files(path)
    if not path.lower().endswith(".obj"):
        return []
    directory = os.path.dirname(os.path.abspath(path))
    found = []
    try:
        with open(path, encoding="utf-8", errors="replace") as source:
            for line in source:
                if line.startswith("mtllib"):
                    name = line[len("mtllib"):].strip()
                    if name:
                        found.append(os.path.normpath(os.path.join(directory, name)))
    except OSError:
        return []
    return found


def converted_path(layout: project.ProjectLayout, source: str, asset: str | None = None) -> str:
    """Where the pipeline keeps the GLB converted from ``source`` (absolute, under ``.editor/``):
    ``assets/models/car.blend`` -> ``.editor/converted/models/car.blend.glb``, and its asset
    ``<guid>`` -> ``.editor/converted/models/car.blend/<guid>.glb``."""
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
    or any file of skeleton and clips alone. A ``.bvh`` is one by its format, converted or not;
    any other source is ``False`` while there is no current GLB to ask: nothing is known yet."""
    if source.lower().endswith(ANIMATION_ONLY):
        return True
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
    if layout is None or (asset is not None and not document_guid.is_text(asset)):
        return None
    glb = converted_path(layout, source, asset)
    return glb if is_current(source, glb, asset) else None


def edit_source_refusal(source: str) -> str:
    """Why the model ``source``, which is no ``.blend``, is not edited from a level, and where it
    is edited instead."""
    name = os.path.basename(source)
    label = _format_label(source)
    return (f"{name} is {_article(label)} {label} file, an interchange format: edit the model in "
            f"the application it came from and export the {label} again; every placement follows.")


def no_mesh_refusal(model: Model) -> str:
    """Why a model whose GLB holds no mesh -- a ``.bvh``, or any file of skeleton and clips
    alone -- has no geometry to edit, and where its clips are authored instead."""
    return (f"{model.label} holds no mesh, only a skeleton and its animation: there "
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
    return (source.lower(), tuple(dependencies),
            document_guid.canonical(asset) if document_guid.is_text(asset) else None)
