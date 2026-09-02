"""Collision-layer contract (port of ``CollisionLayerContract``): ``Layer`` is a single INDEX
rebuilt as ``1u << Layer``, so a multi-bit mask is lossy and :func:`is_multi_layer` exists to
keep that loud."""

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
