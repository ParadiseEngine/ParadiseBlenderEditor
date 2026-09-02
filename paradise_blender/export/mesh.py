"""Mesh GLB export, in entity-local space, deduplicated by mesh datablock.

Two things must hold: the GLB is written with ``export_yup=True``, the same conjugation
:mod:`..contract.axes` applies to transforms (pinned by ``test_axis_parity.py``); and the
object's own transform is neutralized around the export, or the placement is applied twice.
"""

from __future__ import annotations

import contextlib
import hashlib
import os

import bpy
from mathutils import Matrix

from .. import log
from ..paths import ExportPaths
from ..pipeline import glb_textures, ktx
from ..pipeline.cache import ArtifactCache, artifact_cache

__all__ = ["MeshExporter"]

MESH_SUBDIR = "Models"


class MeshExporter:
    """Exports entity meshes to GLB. ``force`` bypasses staleness and the texture cache: the
    one case neither can see is a change to the exporter itself."""

    def __init__(self, force: bool = False) -> None:
        self._fields_by_mesh: dict[str, str] = {}
        #: field -> the key that owns it, so a second key normalising to the same filename is
        #: caught rather than overwriting the first's GLB.
        self._owner_by_field: dict[str, str] = {}
        self._failed: set[str] = set()
        self._warned_no_transcoder = False
        self._force = force
        self._cache: ArtifactCache | None = None

    def resolve_mesh_field(self, obj: bpy.types.Object, paths: ExportPaths) -> str | None:
        """Contract mesh field for an object (authored path wins), exporting on first use."""
        authored = obj.paradise.model_path.strip()
        if authored:
            return self._resolve_authored(obj, authored, paths)

        if obj.type != "MESH" or obj.data is None:
            return None

        mesh_key = _mesh_key(obj)
        if mesh_key in self._fields_by_mesh:
            return self._fields_by_mesh[mesh_key]
        if mesh_key in self._failed:
            return None

        field = self._field_for(mesh_key, obj)
        output_path = paths.output_path_for_field(field)

        if self._force or _is_stale(output_path):
            if not export_object_glb(obj, output_path):
                self._failed.add(mesh_key)
                return None

            # A missing transcoder must be LOUD: it once passed silently and the game refused
            # to launch on GLBs full of PNG. This runs for every GLB on every export (a save
            # invalidates them all), which is why the encode is cached (pipeline/cache.py).
            transcoder = ktx.resolve_transcoder()
            if transcoder is not None:
                glb_textures.externalize(
                    output_path, transcoder, self._texture_cache(paths), self._force
                )
            elif not self._warned_no_transcoder:
                self._warned_no_transcoder = True
                log.warn(
                    "No KTX-Software CLI found (ktx/toktx): exported GLBs keep embedded "
                    "PNG/JPEG and the runtime will REFUSE to load any textured mesh. Install "
                    "KTX-Software or set its path in the add-on preferences, then re-export."
                )

        self._fields_by_mesh[mesh_key] = field
        return field

    def _field_for(self, mesh_key: str, obj: bpy.types.Object) -> str:
        """The GLB field for a mesh key: the datablock's name, plus the modifier signature when
        one applies, plus a disambiguator when another key already normalised to that filename
        (``Cube.001`` and ``Cube_001`` used to overwrite one GLB with no report)."""
        stem = _safe_filename(obj.data.name)
        signature = _modifier_signature(obj)
        if signature:
            stem = f"{stem}.{signature}"
        field = f"{MESH_SUBDIR}/{stem}.glb"
        owner = self._owner_by_field.get(field)
        if owner is not None and owner != mesh_key:
            unique = hashlib.sha1(mesh_key.encode("utf-8")).hexdigest()[:8]
            log.warn(
                f"Mesh '{obj.data.name}' and '{owner.split('#', 1)[0]}' both normalise to "
                f"'{field}'; exporting the second as '{stem}.{unique}.glb'. Rename one of them."
            )
            field = f"{MESH_SUBDIR}/{stem}.{unique}.glb"
        self._owner_by_field[field] = mesh_key
        return field

    def _texture_cache(self, paths: ExportPaths) -> ArtifactCache:
        """The project's artifact cache, resolved lazily since the exporter predates ``ExportPaths``."""
        if self._cache is None:
            self._cache = artifact_cache(paths)
        return self._cache

    def _resolve_authored(
        self, obj: bpy.types.Object, authored: str, paths: ExportPaths
    ) -> str | None:
        """An authored model path as a contract field; only validates reachability."""
        if not authored.lower().endswith((".glb", ".gltf")):
            log.warn(
                f"Entity '{obj.name}' has model path '{authored}', which is not a .glb/.gltf. "
                "The runtime only loads glTF binaries; the reference is ignored."
            )
            return None

        absolute = os.path.abspath(bpy.path.abspath(authored))
        field = paths.data_relative_field(absolute)
        if field is None:
            log.warn(
                f"Entity '{obj.name}' references model '{authored}' outside the data directory. "
                "The runtime resolves meshes under it, so this mesh will not render. Move the "
                "asset there."
            )
        elif not os.path.exists(absolute):
            log.warn(
                f"Entity '{obj.name}' references model '{authored}', which does not exist on "
                "disk. Exporting the reference anyway; the runtime will fail to load it."
            )
        return field


def _is_stale(output_path: str) -> bool:
    """Missing, or older than the .blend (a datablock has no mtime; the save is the proxy).
    "Export only if absent" once left an edited mesh's GLB stale forever while reporting success."""
    if not os.path.exists(output_path):
        return True

    blend_path = bpy.data.filepath
    if not blend_path or not os.path.exists(blend_path):
        # Unsaved .blend: nothing to compare against.
        return False

    return os.path.getmtime(blend_path) > os.path.getmtime(output_path)


def _mesh_key(obj: bpy.types.Object) -> str:
    """What the GLB actually contains: the datablock, and, since ``export_apply`` bakes the
    modifier stack and the skin, the stack that shapes it. Two objects sharing a mesh with
    different modifiers used to get whichever exported first."""
    signature = _modifier_signature(obj)
    return obj.data.name if not signature else f"{obj.data.name}#{signature}"


def _modifier_signature(obj: bpy.types.Object) -> str:
    """A short digest of the modifier stack and the deforming armatures, or ``""`` for a plain
    object. Property VALUES, not just modifier names: two stacks that differ in one setting are
    two different meshes on disk."""
    if not obj.modifiers and not deforming_armatures(obj):
        return ""
    parts: list[str] = []
    for modifier in obj.modifiers:
        if not modifier.show_viewport and not modifier.show_render:
            continue
        parts.append(f"{modifier.type}:{modifier.name}")
        for prop in modifier.bl_rna.properties:
            if prop.is_readonly or prop.identifier in ("name", "show_expanded", "is_active"):
                continue
            value = getattr(modifier, prop.identifier, None)
            if isinstance(value, bpy.types.ID):
                value = value.name
            elif hasattr(value, "__iter__") and not isinstance(value, str):
                value = tuple(value)
            parts.append(f"{prop.identifier}={value!r}")
    parts.extend(f"armature:{armature.name}" for armature in deforming_armatures(obj))
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:8]


def deforming_armatures(obj: bpy.types.Object) -> list[bpy.types.Object]:
    """Armature objects that deform ``obj``, via an Armature modifier or an armature parent."""
    found: list[bpy.types.Object] = []
    for modifier in getattr(obj, "modifiers", []):
        if modifier.type == "ARMATURE" and modifier.object is not None and modifier.object not in found:
            found.append(modifier.object)
    parent = obj.parent
    if parent is not None and parent.type == "ARMATURE" and parent not in found:
        found.append(parent)
    return found


def export_object_glb(obj: bpy.types.Object, output_path: str) -> bool:
    """Export one object's geometry in entity-local space, restoring its transform in a
    ``finally``. The armature must be in the selection: without it the mesh exports with no
    ``skins`` at all, and the first sign is a character that renders but never moves."""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    view_layer = bpy.context.view_layer
    saved_selection = [o for o in bpy.context.selected_objects]
    saved_active = view_layer.objects.active

    exported = [obj, *deforming_armatures(obj)]
    # The ROOT, not `obj`: zeroing a skinned child while its armature keeps its placement
    # bakes the armature's transform in instead of removing it.
    root = next((o for o in exported if o.parent not in exported), obj)
    saved_transform = _capture_transform(root)
    saved_hidden = [o for o in exported if o.hide_get()]

    try:
        # A hidden object cannot be selected; use_selection would then write an empty GLB.
        for hidden in saved_hidden:
            hidden.hide_set(False)

        bpy.ops.object.select_all(action="DESELECT")
        for target in exported:
            target.select_set(True)
        view_layer.objects.active = obj
        root.matrix_world = Matrix.Identity(4)

        bpy.ops.export_scene.gltf(
            filepath=output_path,
            export_format="GLB",
            use_selection=True,
            # Off would leave mesh data Z-up while transforms are Y-up.
            export_yup=True,
            export_apply=True,  # apply modifiers -- the runtime has no modifier stack
            # Blender defaults this OFF, and a normal-mapped mesh without TANGENT renders dark
            # rather than failing (the loader fills a constant tangent): a silent 35% albedo loss.
            export_tangents=True,
            export_cameras=False,
            export_lights=False,
            export_extras=False,
            export_animations=True,
            export_skins=True,
            export_morph=True,
        )
        return True
    except RuntimeError as error:
        log.error(f"Failed to export mesh GLB for '{obj.name}': {error}")
        return False
    finally:
        _restore_transform(root, saved_transform)
        for hidden in saved_hidden:
            hidden.hide_set(True)
        bpy.ops.object.select_all(action="DESELECT")
        for previously in saved_selection:
            # Losing a selection is not worth aborting for.
            with contextlib.suppress(RuntimeError):
                previously.select_set(True)
        view_layer.objects.active = saved_active


def _capture_transform(obj: bpy.types.Object) -> tuple:
    """Snapshot the transform CHANNELS, never ``matrix_world``: assigning a matrix decomposes it
    and the rotation half is lossy at ~1e-6, so one export moved 25 of 321 ShiningPie objects by
    up to 2.2e-6 and the next moved them again, churning every diff and defeating the content-
    keyed navmesh cache. All four rotation representations are saved so the restore is exact in
    any mode."""
    return (
        obj.location.copy(),
        obj.rotation_euler.copy(),
        obj.rotation_quaternion.copy(),
        tuple(obj.rotation_axis_angle),
        obj.scale.copy(),
    )


def _restore_transform(obj: bpy.types.Object, saved: tuple) -> None:
    location, euler, quaternion, axis_angle, scale = saved
    obj.location = location
    obj.rotation_euler = euler
    obj.rotation_quaternion = quaternion
    obj.rotation_axis_angle = axis_angle
    obj.scale = scale


def _safe_filename(name: str) -> str:
    """Make a datablock name safe for a filesystem path.

    Blender allows characters in datablock names that are path separators or invalid on
    Windows. Substituting rather than rejecting keeps the export working; two names that
    normalise identically are told apart by ``MeshExporter._field_for``.
    """
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return safe.strip("._") or "mesh"
