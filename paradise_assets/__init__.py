"""Paradise Assets: ``assets/`` is the source of truth and the ``.blend`` a disposable cache of
one ``*.prefab`` (the inversion of ``paradise_blender``, §2.7). Blender owns placement, the
document owns component data (passed through untouched), the GLB owns geometry.

No ``bpy`` at module scope: Python runs ``__init__`` before any submodule, and the
``document/`` unit tests are the only defence keeping the canonical-TOML writer byte-identical
to the C# one.
"""

from __future__ import annotations

__all__ = ["register", "unregister"]

_REGISTERED: list = []


def register() -> None:
    import bpy

    from . import browser, component_ops, dropped, field_widgets, ops, prefs, ui, watch
    from .materialize import sync
    from .play import ops as play_ops

    # Preferences first (unregistered reads as "nothing configured"); widgets and operators
    # before ui, or the panel draws dead buttons rather than failing loudly.
    for cls in (
        *prefs.classes, *ops.classes, *play_ops.classes, *field_widgets.classes,
        *component_ops.classes, *ui.classes, *browser.classes,
    ):
        bpy.utils.register_class(cls)
        _REGISTERED.append(cls)

    field_widgets.attach()

    # The Asset Browser's drop cannot be replaced, only followed (see dropped.py).
    dropped.register_handler()

    # Ctrl+S writes the document too (materialize/sync.py).
    sync.register_handler()
    watch.register_handler()

    # After the classes: the menu draws the operator, so it must exist by the time anyone opens it.
    browser.register_menu()


def unregister() -> None:
    import bpy

    from . import browser, dropped, field_widgets, watch
    from .materialize import sync

    browser.unregister_menu()
    dropped.unregister_handler()
    sync.unregister_handler()
    watch.unregister_handler()
    field_widgets.detach()

    # Reverse order, or Blender warns about an unregistered parent panel.
    while _REGISTERED:
        bpy.utils.unregister_class(_REGISTERED.pop())
