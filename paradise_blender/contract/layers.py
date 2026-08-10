"""Collision-layer contract -- port of ``Paradise.Export.Geometry.CollisionLayerContract``.

``ColliderShapeData.Layer`` is a Unity-style single layer **index**; consumers rebuild the
membership mask as ``1u << Layer``. A single int therefore cannot express multi-layer
membership, so a mask with several bits set is lossy: only the lowest bit survives.

Blender has no built-in collision-layer concept, so ``authoring/collider.py`` exposes a
20-slot boolean mask (mirroring Godot's ``collision_layer`` UI) and funnels it through here.
Keeping the lossy case *loud* rather than silent is the whole point of :func:`is_multi_layer`
-- the Godot exporter warns in exactly the same situation, and an author who has ticked two
layers needs to know the runtime will only see one.
"""

from __future__ import annotations

__all__ = ["is_multi_layer", "layer_index_to_mask", "mask_to_layer_index"]


def mask_to_layer_index(mask: int) -> int:
    """Index of the lowest set bit (mask 1 -> 0, mask 2 -> 1). An unlayered body maps to 0."""
    mask &= 0xFFFFFFFF
    return 0 if mask == 0 else (mask & -mask).bit_length() - 1


def is_multi_layer(mask: int) -> bool:
    """True when more than one layer bit is set -- :func:`mask_to_layer_index` is lossy here."""
    mask &= 0xFFFFFFFF
    return (mask & (mask - 1)) != 0


def layer_index_to_mask(index: int) -> int:
    """The consumer-side inverse (``1u << Layer``), used by round-trip tests."""
    return 1 << index if 0 <= index < 32 else 0
