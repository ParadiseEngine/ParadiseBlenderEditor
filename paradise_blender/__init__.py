"""Paradise Engine Tools -- author, play, and live-preview Paradise scenes from Blender.

Companion to ``ParadiseGodotEditor``: both write the same engine-neutral scene contract
(``Paradise.Export``'s ``LevelData``), so a project can be authored in either tool and run by
the same runtime.

**This module imports no Blender API at module scope.** Everything that needs ``bpy`` is
imported inside :func:`register`. That is what lets ``paradise_blender.contract`` -- the pure
contract implementation -- be imported and unit-tested with plain ``pytest``, outside Blender.
Since Python executes a package's ``__init__`` before any submodule, a single top-level
``import bpy`` here would make the whole subpackage untestable without Blender, and the
contract tests are the main defence against silently drifting from the C# implementation.

Registration order is not arbitrary, and :func:`register` is written to make that hard to
break:

1. **Property-group classes before the pointers that reference them.** Blender resolves
   ``PointerProperty(type=X)`` at assignment time, so ``X`` must already be registered.
2. **Nested groups before their containers** -- ``ColliderReference`` before the entity group
   that holds a ``CollectionProperty`` of it.
3. **Handlers last, and removed first on unregister.** A handler that survives unregistration
   holds a reference to a class Blender has already torn down, and the resulting crash points
   at the wrong place entirely.

Unregistration runs in exact reverse, including stopping any live-preview session -- a live
socket, its two worker threads, and its timer would otherwise outlive the addon.
"""

from __future__ import annotations

from . import log

__all__ = ["register", "unregister"]

# Populated by register() so unregister() tears down exactly what was set up, even if an
# import list changes between the two calls (an addon reload during development).
_registered_classes: list = []
_registered_pointer_modules: list = []
_registered_handler_modules: list = []


def _collect():
    """Import the Blender-dependent modules and return what needs registering.

    Deferred to call time -- see the module docstring for why this is not at module scope.
    """
    from . import prefs
    from .authoring import authored_components, material_props, world_props
    from .authoring import collider as authoring_collider
    from .authoring import entity as authoring_entity
    from .authoring import guid as authoring_guid
    from .authoring import ops as authoring_ops
    from .export import ops as export_ops
    from .live import ops as live_ops
    from .pipeline import schema_build
    from .play import ops as play_ops
    from .ui import panels

    classes = [
        # Property groups first: the pointer assignments below reference them by type.
        *authoring_entity.classes,
        *authoring_collider.classes,
        *material_props.classes,
        *world_props.classes,
        *prefs.classes,
        # Then operators, then the panels that invoke them.
        *authoring_ops.classes,
        *authored_components.classes,
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

    handler_modules = [authoring_guid, export_ops, schema_build]

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

    # A running preview owns a socket, two threads, and a timer. Stopping it first means none
    # of them can call into classes that are about to be unregistered.
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
