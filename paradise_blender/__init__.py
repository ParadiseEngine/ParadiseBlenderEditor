"""Paradise Engine Tools: author, play and live-preview scenes from Blender (the .blend is truth;
see #35 for this addon's status beside ``paradise_assets``).

No ``bpy`` import at module scope: Python runs a package's ``__init__`` before any submodule,
so one here would make ``paradise_blender.contract`` untestable under pytest, the main defence
against drifting from the C# contract. Registration order: property groups before the pointers
that reference them (Blender resolves ``PointerProperty(type=X)`` at assignment), nested groups
before their containers, handlers last and removed first.
"""

from __future__ import annotations

from . import log

__all__ = ["register", "unregister"]

# Populated by register() so unregister() tears down exactly what was set up across a reload.
_registered_classes: list = []
_registered_pointer_modules: list = []
_registered_handler_modules: list = []


def _collect():
    """Import the Blender-dependent modules at call time (module docstring)."""
    from . import prefs
    from .authoring import authored_components, config_store, material_props, world_props
    from .authoring import collider as authoring_collider
    from .authoring import entity as authoring_entity
    from .authoring import model_preview as authoring_model_preview
    from .authoring import ops as authoring_ops
    from .export import ops as export_ops
    from .live import ops as live_ops
    from .pipeline import schema_build
    from .play import ops as play_ops
    from .ui import panels

    classes = [
        *authoring_entity.classes,
        *authoring_collider.classes,
        *material_props.classes,
        *world_props.classes,
        *prefs.classes,
        *authoring_ops.classes,
        *authoring_model_preview.classes,
        *authored_components.classes,
        *config_store.classes,
        *export_ops.classes,
        *play_ops.classes,
        *live_ops.classes,
        *panels.classes,
    ]

    pointer_modules = [
        authoring_entity,
        authoring_collider,
        material_props,
        world_props,
        prefs,
    ]

    handler_modules = [export_ops, schema_build]

    return classes, pointer_modules, handler_modules


def register() -> None:
    import bpy

    from .contract import SCHEMA_VERSION

    classes, pointer_modules, handler_modules = _collect()

    for cls in classes:
        bpy.utils.register_class(cls)
    _registered_classes[:] = classes

    for module in pointer_modules:
        module.register_pointers()
    _registered_pointer_modules[:] = pointer_modules

    for module in handler_modules:
        module.register()
    _registered_handler_modules[:] = handler_modules

    log.info(f"Paradise Engine Tools registered (contract schema v{SCHEMA_VERSION}).")


def unregister() -> None:
    import bpy

    # Stop the preview first so its threads cannot call into unregistered classes.
    try:
        from .live import session as live_session

        live_session.stop()
    except Exception as error:  # never block unregistration
        log.warn(f"Failed to stop the live preview cleanly: {error}")

    for module in reversed(_registered_handler_modules):
        module.unregister()
    _registered_handler_modules.clear()

    for module in reversed(_registered_pointer_modules):
        module.unregister_pointers()
    _registered_pointer_modules.clear()

    for cls in reversed(_registered_classes):
        bpy.utils.unregister_class(cls)
    _registered_classes.clear()
