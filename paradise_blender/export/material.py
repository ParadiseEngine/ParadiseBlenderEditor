"""Blender materials -> ``LevelMaterialData``.

Counterpart to ``ParadiseGodotEditor``'s ``MaterialExporter``, reading a Principled BSDF node
tree instead of a Godot ``BaseMaterial3D``.

**The colour-space rule is inverted relative to the Godot host, and this is the single easiest
thing to get wrong in the whole addon.** The Godot exporter calls ``Color.SrgbToLinear()`` on
every authored colour, because Godot stores authored colours as sRGB while the contract is
linear. Blender does not: socket ``default_value`` colours are already **linear scene-referred**
floats -- the colour picker merely *displays* them through a transform. So they are passed
through verbatim. Applying ``srgb_to_linear`` here would darken every material in the scene by
roughly the gamma curve, and it would look like a plausible lighting difference rather than a
bug.

The exception is anything authored through a widget that means sRGB by convention -- the
procedural recipe tints in :mod:`..authoring.material_props`, which mirror Godot metadata and
are documented as sRGB. Those are linearized explicitly, and the call site says so.

Texture references are the *source* image path relative to ``data/``. Transcoding to KTX2 is
the asset pipeline's job (:mod:`..pipeline.ktx`), not this exporter's.
"""

from __future__ import annotations

import os

import bpy

from .. import log
from ..authoring.material_props import resolved_material_kind
from ..contract.color import Color32
from ..contract.schema import LevelMaterialData
from ..contract.writer import write_json_document
from ..paths import ExportPaths, material_file_field

__all__ = ["MaterialExporter"]


class MaterialExporter:
    """Collects materials referenced by exported entities and writes one JSON per material.

    Deduplicates by contract field, because the field -- not the datablock -- is the identity
    the runtime uses. Two differently-named materials can still collide on a field (the field
    is derived from the basename), so a collision is reported rather than silently dropped.
    """

    def __init__(self) -> None:
        self._exported: dict[str, LevelMaterialData] = {}
        self._field_source: dict[str, str] = {}

    def export_material_slots(self, obj: bpy.types.Object) -> list[str | None]:
        """Contract material fields for an object's slots, in slot order.

        Slot order is load-bearing: the contract requires the referenced GLB's primitive order
        to match this list, and Blender's glTF exporter emits one primitive per used material
        slot in slot order. A ``None`` entry means "no material" -- the GLB's own embedded
        material is authoritative for that primitive.
        """
        slots: list[str | None] = []
        for slot in obj.material_slots:
            slots.append(self._register(slot.material))
        return slots

    def write_exported_materials(self, paths: ExportPaths) -> int:
        for material in self._exported.values():
            write_json_document(paths.output_path_for_field(material.path), material.to_json())
        return len(self._exported)

    def _register(self, material: bpy.types.Material | None) -> str | None:
        if material is None:
            return None

        source = material.name
        field = material_file_field(source)

        if field in self._exported:
            existing = self._field_source.get(field)
            if existing is not None and existing != source:
                log.warn(
                    f"Material name collision: '{source}' and '{existing}' both map to '{field}'; "
                    "keeping the first. Rename one of them."
                )
            return field

        self._exported[field] = self._to_level_material(field, material)
        self._field_source[field] = source
        return field

    def _to_level_material(self, field: str, material: bpy.types.Material) -> LevelMaterialData:
        extra = material.paradise
        data = LevelMaterialData(path=field, name=material.name)

        bsdf = _find_principled(material)
        if bsdf is None:
            # A material with no Principled BSDF (pure node-group, emission-only, or nodes
            # disabled) still needs a contract entry, so fall back to the viewport display
            # colour rather than exporting nothing and rendering the object black.
            log.warn(
                f"Material '{material.name}' has no Principled BSDF; exporting its viewport "
                "display colour only. Node-based shading is not translated."
            )
            r, g, b, a = material.diffuse_color
            data.base_color_factor = Color32.from_rgba(r, g, b, a)
            data.metallic_factor = material.metallic
            data.roughness_factor = material.roughness
        else:
            _read_principled(bsdf, data)

        data.alpha_mode = _alpha_mode(material, data)
        data.normal_texture = _normal_texture(bsdf) if bsdf is not None else None
        data.normal_scale = _normal_scale(bsdf) if bsdf is not None else 1.0

        # Contract-only parameters. These are authored as sRGB (mirroring the Godot host's
        # metadata convention), so unlike the BSDF colours above they DO get linearized.
        data.transmission_factor = min(max(extra.transmission_factor, 0.0), 1.0)
        data.material_kind = resolved_material_kind(extra)
        data.emissive_strength = max(0.0, extra.emissive_strength)
        data.noise_scale = extra.noise_scale
        data.flow_speed = extra.flow_speed
        data.color_a = Color32.from_srgb(*extra.color_a, 1.0)
        data.color_b = Color32.from_srgb(*extra.color_b, 1.0)

        return data


def _find_principled(material: bpy.types.Material) -> bpy.types.Node | None:
    """The Principled BSDF actually feeding the output, if there is one.

    Walks back from the Material Output rather than taking the first Principled node in the
    tree: a scene often carries disconnected leftovers, and picking one of those would export
    a material nobody can see.
    """
    # `use_nodes` is deprecated as of Blender 5.x (slated for removal in 6.0), where a material
    # is always node-based. Treat its absence as "yes, nodes" rather than assuming either way.
    if material.node_tree is None or not getattr(material, "use_nodes", True):
        return None

    output = next(
        (n for n in material.node_tree.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output),
        None,
    )
    if output is not None:
        surface = output.inputs.get("Surface")
        if surface is not None and surface.is_linked:
            linked = surface.links[0].from_node
            if linked.type == "BSDF_PRINCIPLED":
                return linked

    return next((n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)


def _read_principled(bsdf: bpy.types.Node, data: LevelMaterialData) -> None:
    base_color = bsdf.inputs.get("Base Color")
    if base_color is not None:
        # Linear already -- see the module docstring. No transfer function here.
        r, g, b, a = base_color.default_value
        alpha_input = bsdf.inputs.get("Alpha")
        alpha = alpha_input.default_value if alpha_input is not None else a
        data.base_color_factor = Color32.from_rgba(r, g, b, alpha)
        data.base_color_texture = _texture_path(base_color)

    metallic = bsdf.inputs.get("Metallic")
    roughness = bsdf.inputs.get("Roughness")
    if metallic is not None:
        data.metallic_factor = metallic.default_value
    if roughness is not None:
        data.roughness_factor = roughness.default_value

    # The contract carries ONE metallic-roughness map (glTF packs both into a single texture).
    # Blender routes them through separate sockets, so whichever is textured wins; if both are
    # and they differ, the author has not packed an ORM map and needs to know.
    metallic_texture = _texture_path(metallic) if metallic is not None else None
    roughness_texture = _texture_path(roughness) if roughness is not None else None
    if metallic_texture and roughness_texture and metallic_texture != roughness_texture:
        log.warn(
            f"Material '{data.name}' drives Metallic and Roughness from different images "
            f"('{metallic_texture}', '{roughness_texture}'). The contract stores one packed "
            "metallic-roughness map; only the Metallic one is exported."
        )
    data.metallic_roughness_texture = metallic_texture or roughness_texture

    emission = bsdf.inputs.get("Emission Color")
    strength = bsdf.inputs.get("Emission Strength")
    if emission is not None:
        multiplier = strength.default_value if strength is not None else 1.0
        r, g, b, _ = emission.default_value
        # The multiplier is applied in linear space (where it belongs); the 8-bit Color32
        # then clamps, so a strongly emissive material relies on emissive_strength for HDR.
        data.emissive_factor = Color32.from_rgba(r * multiplier, g * multiplier, b * multiplier, 1.0)
        data.emissive_texture = _texture_path(emission)


def _normal_texture(bsdf: bpy.types.Node) -> str | None:
    """Follow Normal -> Normal Map -> Image Texture.

    A normal map is a two-hop chain in Blender (unlike colour sockets), and the intermediate
    Normal Map node is where the strength lives.
    """
    normal_input = bsdf.inputs.get("Normal")
    if normal_input is None or not normal_input.is_linked:
        return None
    node = normal_input.links[0].from_node
    if node.type != "NORMAL_MAP":
        return None
    return _texture_path(node.inputs.get("Color"))


def _normal_scale(bsdf: bpy.types.Node) -> float:
    normal_input = bsdf.inputs.get("Normal")
    if normal_input is None or not normal_input.is_linked:
        return 1.0
    node = normal_input.links[0].from_node
    if node.type != "NORMAL_MAP":
        return 1.0
    strength = node.inputs.get("Strength")
    return strength.default_value if strength is not None else 1.0


def _alpha_mode(material: bpy.types.Material, data: LevelMaterialData) -> str:
    """Map Blender's blend settings to the contract's glTF-style alpha mode.

    Mirrors the Godot exporter's precedence: a non-opaque base colour alpha forces Blend
    regardless of the material's declared blend mode, because an author who dialled alpha down
    means it to be transparent.
    """
    if data.base_color_factor.a < 0.999:
        return "Blend"

    # Blender 4.2+ replaced blend_method's many values with OPAQUE/BLEND/DITHERED; older
    # files may still carry CLIP/HASHED. Handle both rather than assuming a version.
    method = getattr(material, "blend_method", "OPAQUE")
    if method in {"CLIP", "HASHED", "DITHERED"}:
        return "Mask"
    if method == "BLEND":
        return "Blend"
    return "Opaque"


def _texture_path(socket) -> str | None:  # bpy.types.NodeSocket | None
    """Data-relative path of the image feeding a socket, or ``None``.

    Returns ``None`` for generated/packed images with no file on disk: the runtime loads
    textures from files under ``data/``, so referencing an image that exists only inside the
    .blend would produce a path that never resolves.
    """
    if socket is None or not socket.is_linked:
        return None

    node = socket.links[0].from_node
    if node.type != "TEX_IMAGE" or node.image is None:
        return None

    image = node.image
    if not image.filepath:
        log.warn(
            f"Image '{image.name}' is packed or generated and has no file on disk, so it "
            "cannot be referenced by the contract. Save it under the data directory."
        )
        return None

    absolute = os.path.abspath(bpy.path.abspath(image.filepath))
    from ..prefs import export_paths

    paths = export_paths(bpy.context.scene)
    field = paths.data_relative_field(absolute)
    if field is None:
        # Not the alarm it used to be: mesh GLBs carry their textures regardless (embedded at
        # export, externalized to KTX2 sidecars next to the GLB), so an authoring-side source —
        # e.g. authoring/textures/ in a game repo — renders fine. Only the material DOCUMENT
        # omits the reference, which matters solely to hosts that read document textures.
        log.info(
            f"Texture '{image.filepath}' is outside the data directory; the material document "
            "omits it (the mesh GLB still carries the texture via its KTX2 sidecars)."
        )
    return field
