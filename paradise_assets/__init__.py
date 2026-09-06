"""Paradise Assets: ``assets/`` is the source of truth and the ``.blend`` a disposable cache of
one ``*.prefab``. Blender owns placement, the document owns component data (passed through
untouched), the GLB owns geometry.

No ``bpy`` at module scope: Python runs ``__init__`` before any submodule, and the
``document/`` unit tests are the only defence keeping the canonical-TOML writer byte-identical
to the C# one.
"""

from __future__ import annotations

__all__ = ["register", "unregister"]

_REGISTERED: list = []


def register() -> None:
    import bpy

    from . import browser, component_ops, context_menu, dropped, field_widgets, ops, prefs, ui, watch
    from .materialize import sync
    from .play import ops as play_ops

    # Blender keeps whatever a register() that raised had already registered, and every enable
    # after that dies on "already registered as a subclass" -- so a failure has to unwind itself
    # or the addon cannot be turned back on without restarting Blender.
    try:
        # Preferences first (unregistered reads as "nothing configured"); widgets and operators
        # before ui, or the panel draws dead buttons rather than failing loudly.
        for cls in (
            *prefs.classes, *ops.classes, *play_ops.classes, *field_widgets.classes,
            *component_ops.classes, *ui.classes, *browser.classes, *context_menu.classes,
        ):
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)

        field_widgets.attach()

        # After the classes (the menu draws the operator, so it must exist by the time anyone
        # opens it) and before the handlers, so a handler that fails cannot cost us the menu.
        browser.register_menu()
        context_menu.register_menu()

        # The Asset Browser's drop cannot be replaced, only followed (see dropped.py).
        dropped.register_handler()

        # Ctrl+S writes the document too (materialize/sync.py).
        sync.register_handler()
        watch.register_handler()
    except Exception:
        unregister()
        raise


def unregister() -> None:
    import bpy

    from . import browser, context_menu, dropped, field_widgets, watch
    from .materialize import sync
    from .play import session as play_session
    browser.unregister_menu()
    context_menu.unregister_menu()
    dropped.unregister_handler()
    sync.unregister_handler()
    watch.unregister_handler()
    play_session.stop_all()
    field_widgets.detach()

    # Reverse order, or Blender warns about an unregistered parent panel.
    while _REGISTERED:
        bpy.utils.unregister_class(_REGISTERED.pop())
