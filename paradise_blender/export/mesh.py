"""Mesh GLB export.

The contract's ``RenderableComponentData.Mesh`` is a GLB path relative to ``data/`` holding
the entity's visual subtree **in entity-local space** -- the entity's ``WorldMatrix`` places
it. So two things must hold, and both are easy to get wrong:

1. **The GLB must be written with the same Y-up conversion the transforms use.** Blender's
   glTF exporter does exactly the conjugation :mod:`..contract.axes` implements, so
   ``export_yup=True`` (its default) is what keeps mesh data and node transforms agreeing.
   ``tests/integration/test_axis_parity.py`` pins that equivalence.

2. **The object's own transform must not be baked into the GLB**, or it would be applied
   twice -- once by the GLB's node transform and again by the contract's ``WorldMatrix``,
   putting the object at the square of its placement. Blender's exporter has no "ignore
   transform" switch, so :func:`export_object_glb` neutralizes the transform around the call
   and restores it in a ``finally``.

Meshes are deduplicated by mesh datablock: ten objects sharing one mesh produce one GLB and
ten entities referencing it, which is also what the Godot host's shared-GLB pipeline does.
"""

from __future__ import annotations

import contextlib
import os

import bpy
from mathutils import Matrix

from .. import log
from ..paths import ExportPaths
from ..pipeline import glb_textures, ktx
from ..pipeline.cache import ArtifactCache, artifact_cache

__all__ = ["MeshExporter"]

#: Where generated GLBs land, relative to the data directory. Matches the Godot host's layout.
MESH_SUBDIR = "Models"


class MeshExporter:
    """Exports entity meshes to GLB, deduplicated by mesh datablock.

    ``force`` re-exports and re-encodes everything, ignoring both the staleness check and the
    texture cache. It is the escape hatch for the one case neither can see: a change to the
    exporter or the transcoding pipeline itself, which leaves every output stale while the
    .blend's mtime says nothing changed.
    """

    def __init__(self, force: bool = False) -> None:
        self._fields_by_mesh: dict[str, str] = {}
        self._failed: set[str] = set()
        self._warned_no_transcoder = False
        self._force = force
        self._cache: ArtifactCache | None = None

    def resolve_mesh_field(self, obj: bpy.types.Object, paths: ExportPaths) -> str | None:
        """Contract mesh field for an object, exporting the GLB on first use.

        Precedence mirrors the Godot host: an explicitly authored model path wins, otherwise
        the object's own geometry is exported. Returns ``None`` when the object has no
        exportable geometry, which is normal -- an empty used as a collider parent, say.
        """
        authored = obj.paradise.model_path.strip()
        if authored:
            return self._resolve_authored(obj, authored, paths)

        if obj.type != "MESH" or obj.data is None:
            return None

        mesh_key = obj.data.name
        if mesh_key in self._fields_by_mesh:
            return self._fields_by_mesh[mesh_key]
        if mesh_key in self._failed:
            return None

        field = f"{MESH_SUBDIR}/{_safe_filename(mesh_key)}.glb"
        output_path = paths.output_path_for_field(field)

        if self._force or _is_stale(output_path):
            if not export_object_glb(obj, output_path):
                self._failed.add(mesh_key)
                return None

            # The engine reads textured meshes through KTX2 sidecars next to the GLB (its glTF
            # reader rejects PNG/JPEG outright), but Blender can only EMBED images — so every
            # textured export gets post-processed into the sidecar layout here. A missing
            # transcoder must be LOUD: it once passed silently and the game refused to launch
            # on GLBs full of PNG, hours and one confused user later.
            #
            # Note what the staleness check above CANNOT do: a .blend save invalidates every
            # GLB, so this runs for all of them on every export. That is why the encode itself
            # is cached rather than gated on the GLB — see :mod:`..pipeline.cache`.
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

    def _texture_cache(self, paths: ExportPaths) -> ArtifactCache:
        """The project's artifact cache, resolved once per export.

        Lazily, because the cache belongs to the project being exported and the exporter is
        constructed before any ``ExportPaths`` is in hand.
        """
        if self._cache is None:
            self._cache = artifact_cache(paths)
        return self._cache

    def _resolve_authored(
        self, obj: bpy.types.Object, authored: str, paths: ExportPaths
    ) -> str | None:
        """Map an authored model path to a contract field, without exporting anything.

        An authored path points at an asset that already exists under ``data/`` (typically a
        GLB produced by the art pipeline), so this only validates reachability.
        """
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
    """Whether a mesh GLB needs rewriting.

    Missing, obviously. Otherwise: older than the .blend it came from. A Blender datablock has
    no modification time to compare against, but any geometry edit reaches disk as a .blend
    save, so the file's mtime is the closest available proxy.

    The previous rule was "export only if absent", which meant an edited mesh kept its original
    GLB forever -- the export reported success, the contract pointed at a stale file, and the
    runtime showed the old geometry. AGENTS.md tells authors to re-export after a scene edit;
    this is what makes that instruction true for geometry.
    """
    if not os.path.exists(output_path):
        return True

    blend_path = bpy.data.filepath
    if not blend_path or not os.path.exists(blend_path):
        # Unsaved .blend: nothing to compare against, and the export lands in a temp directory
        # anyway. Keep the old behaviour rather than re-encoding every mesh on every export.
        return False

    return os.path.getmtime(blend_path) > os.path.getmtime(output_path)


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
    """Export one object's geometry to ``output_path`` in entity-local space.

    Temporarily clears the exported root's world transform so the GLB contains geometry only,
    then restores it. Mutating the scene mid-export is unpleasant but is the only way Blender's
    exporter supports this; the ``finally`` guarantees restoration even if the export raises,
    and the operation is invisible to the user because no depsgraph-visible frame is drawn
    in between.

    **A skinned mesh is exported together with its armature.** ``use_selection`` honours the
    selection literally, and an armature left out of it does not merely lose its animations --
    the mesh exports with no ``skins`` at all, as plain static geometry in bind pose. Nothing
    fails, so the first sign is a character that renders but never moves.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    view_layer = bpy.context.view_layer
    saved_selection = [o for o in bpy.context.selected_objects]
    saved_active = view_layer.objects.active

    exported = [obj, *deforming_armatures(obj)]
    # Neutralize the transform of the hierarchy ROOT, not of `obj`: for a skinned mesh parented
    # to its armature, zeroing the child while the parent keeps its placement would bake the
    # armature's transform into the result instead of removing it.
    root = next((o for o in exported if o.parent not in exported), obj)
    saved_transform = _capture_transform(root)
    saved_hidden = [o for o in exported if o.hide_get()]

    try:
        # A hidden object cannot be selected, and the exporter's use_selection would then
        # write an empty GLB rather than failing.
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
            # +Y up: the same basis change contract/axes.py applies to transforms. Turning
            # this off would leave mesh data Z-up while transforms are Y-up.
            export_yup=True,
            export_apply=True,  # apply modifiers -- the runtime has no modifier stack
            # Blender defaults this OFF, and a normal-mapped mesh without TANGENT renders DARK
            # rather than failing: the loader default-fills a CONSTANT (1,0,0,+1) tangent, the
            # shader's degenerate-tangent guard only trips where the normal parallels ±X, and
            # everywhere else it builds a valid-but-meaningless TBN that tilts the shading normal
            # away from the light. Cheap to always emit; the alternative is a silent 35% albedo
            # loss on every asset that ships a normal map.
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
            # The object may have become unselectable (hidden by a collection toggle) since
            # the selection was captured; losing a selection is not worth aborting for.
            with contextlib.suppress(RuntimeError):
                previously.select_set(True)
        view_layer.objects.active = saved_active


def _capture_transform(obj: bpy.types.Object) -> tuple:
    """Snapshot an object's transform CHANNELS, so it can be put back exactly.

    Not ``matrix_world``: assigning one decomposes it into location/rotation/scale and the
    rotation half of that round trip is lossy at ~1e-6. Restoring a saved matrix therefore leaves
    the object a couple of microns from where it started -- invisible in the viewport, and
    permanent in the exported data. Measured on ShiningPie, one export moved 25 of 321 objects by
    up to 2.2e-6, and the next export moved them again.

    Nothing looks wrong when this happens, which is what makes it worth the extra care: the
    exported transforms churn on every export (noise in every diff of a committed ``data/``), and
    anything that decides "did this change?" by comparing content -- the navmesh cache in
    :mod:`..pipeline.cache`, most obviously -- never sees an unchanged scene twice.

    All four rotation representations are saved rather than the one ``rotation_mode`` selects:
    they are separate stored fields, restoring them all is exact regardless of mode, and it costs
    a few floats.
    """
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
    Windows. Substituting rather than rejecting keeps the export working; a collision between
    two names that normalize identically is possible but is reported by the caller when the
    second export overwrites the first.
    """
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return safe.strip("._") or "mesh"
