"""Preflight a new GLB and the files the CLI will extract from it.

The engine owns extraction. Reading its documented directory rules here is only for refusing
collisions before any source is written, and locating the prefab seed to move to the chosen path.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass

from . import new_prefab, schema
from .project import ProjectLayout

#: What ``paradise assets extract`` writes for a GLB, each routed by the project's ``[extract]``.
EXTRACTED_KINDS = ("meshes", "materials", "textures", "prefabs")


@dataclass(frozen=True)
class GeometryTarget:
    prefab: str
    model: str
    seed: str


def prepare(path: str, layout: ProjectLayout) -> GeometryTarget:
    path = os.path.abspath(path)
    new_prefab.refuse_target(path, layout)
    if not os.path.isfile(os.path.join(layout.editor, "authoring-schema.json")) or not any(
        "skinned" not in item.type_name.lower() for item in schema.mesh_components(layout.root)
    ):
        raise new_prefab.CreateError(
            "Build the game's launcher first: .editor/authoring-schema.json must declare a "
            "static mesh component before geometry can become a renderable prefab."
        )
    stem = os.path.splitext(os.path.basename(path))[0]
    model = os.path.splitext(path)[0] + ".glb"
    new_prefab.refuse_target(model, layout)
    directories = extraction_directories(layout, os.path.dirname(path))
    for destination in directories.values():
        # The same boundary check also rejects symlinked output folders outside the project.
        new_prefab.refuse_target(os.path.join(destination, stem + ".prefab"), layout)

    for directory in set(directories.values()):
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            if name.casefold().startswith((stem.casefold() + ".", stem.casefold() + "_")):
                raise new_prefab.CreateError(
                    f"{layout.relative(os.path.join(directory, name))} already exists in the "
                    "model's extraction namespace. Pick another prefab name to keep its assets."
                )

    return GeometryTarget(path, model, os.path.join(directories["prefabs"], stem + ".prefab"))


def extraction_directories(
    layout: ProjectLayout, beside: str, kinds: tuple[str, ...] = EXTRACTED_KINDS
) -> dict[str, str]:
    """Where the engine routes each kind it extracts from a GLB in the folder ``beside``: the
    project's ``[extract]`` entry for that kind, else its ``directory``, else the GLB's own
    folder -- the documented rule, read so a collision is refused before any file is written."""
    with open(layout.manifest, "rb") as handle:
        extraction = tomllib.load(handle).get("extract", {})
    if not isinstance(extraction, dict):
        raise new_prefab.CreateError("The project's [extract] settings must be a TOML table.")

    directories = {}
    for kind in kinds:
        directory = extraction.get(kind, extraction.get("directory"))
        if directory is not None and not isinstance(directory, str):
            raise new_prefab.CreateError(f"[extract].{kind} must name an assets-relative directory.")
        directories[kind] = os.path.abspath(layout.resolve(directory) if directory is not None else beside)
    return directories
