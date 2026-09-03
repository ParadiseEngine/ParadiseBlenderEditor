"""``data/ProjectSettings.json``: runtime tuning with no Blender counterpart, so the contract
defaults are written. Written at all so both hosts produce the same file set.
"""

from __future__ import annotations

from typing import Any

from ..contract.writer import write_json_document
from ..paths import ExportPaths

__all__ = ["build_project_settings", "export_project_settings"]

#: ``ProjectSettingsData.CurrentSchemaVersion``.
SCHEMA_VERSION = 1


def build_project_settings() -> dict[str, Any]:
    """The contract defaults, written explicitly so the document is self-describing."""
    return {
        "SchemaVersion": SCHEMA_VERSION,
        "Physics": {
            "CollisionMatrix": {"LayerMasks": []},
            "Dynamics": {
                "MinSpeed": 0.005,
                "Skin": 0.02,
                "PushStrength": 1.2,
                "DefaultStaticRestitution": 0.4,
                # Must stay negative: a positive value silently inverts gravity.
                "GravityY": -9.81,
                "StaticFriction": 0.2,
                "MinAngularSpeed": 0.05,
            },
        },
        "Rendering": {
            "RenderScale": 1.0,
            "MsaaSamples": 1,
            "AnisotropicLevel": 16,
            "SpecularAaVariance": 0.5,
            "SpecularAaClamp": 0.25,
        },
    }


def export_project_settings(paths: ExportPaths) -> str:
    output_path = paths.project_settings_output_path()
    write_json_document(output_path, build_project_settings())
    return output_path
