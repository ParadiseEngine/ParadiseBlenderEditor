"""``data/ProjectSettings.json``.

Physics-solver and renderer tuning the runtime reads as a puppet config. Every value in
``ProjectSettingsData`` is a runtime tuning constant with no Blender counterpart -- Blender's
own physics solver settings describe a different solver, and its render settings describe a
different renderer. So this writes the contract defaults.

Emitting the file at all (rather than letting the runtime fall back internally) matters
because the Godot host writes it too: a project exported from either tool then has the same
file set, and switching authoring tools does not change runtime behaviour.
"""

from __future__ import annotations

from typing import Any

from ..contract.writer import write_json_document
from ..paths import ExportPaths

__all__ = ["build_project_settings", "export_project_settings"]

#: ``ProjectSettingsData.CurrentSchemaVersion``.
SCHEMA_VERSION = 1


def build_project_settings() -> dict[str, Any]:
    """The contract defaults, matching ``PhysicsDynamicsSettingsData`` / ``RenderSettingsData``.

    Written out explicitly rather than omitted so the document is self-describing: a reader
    can see what the runtime will use without knowing the C# defaults.
    """
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
