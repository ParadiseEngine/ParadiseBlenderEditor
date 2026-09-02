"""Paradise Assets -- open ``assets/scenes/*.scene`` in Blender and place things in it.

The INVERSION of what ``paradise_blender`` does, which is why it is a separate addon rather than
a feature of that one. There, the ``.blend`` is the source of truth and the scene is exported to
``data/``. Here, ``assets/`` is the committed source of truth and the ``.blend`` is a disposable
cache of one document in it -- the direction the asset-management plan's §2.7 calls the addon
inversion. Both can be installed at once during the migration.

The division of ownership, which every module here follows:

* **Blender owns placement** -- names, parents, transforms, which objects exist.
* **The document owns component data**, and this addon passes it through untouched. That is what
  lets a scene full of components it has never heard of be opened and saved safely.
* **The GLB owns geometry.** Meshes are instanced, not editable in place.

``bpy`` MUST NOT be imported at module scope. Python runs a package's ``__init__`` before any
submodule, so a top-level ``import bpy`` here would make :mod:`paradise_assets.document`
unimportable outside Blender -- and those unit tests are the only thing standing between this
addon's canonical-TOML writer and the C# one it has to match byte for byte.
"""

from __future__ import annotations

__all__ = ["register", "unregister"]

_REGISTERED: list = []


def register() -> None:
    import bpy

    from . import browser, component_ops, dropped, field_widgets, ops, prefs, ui, watch
    from .materialize import sync
    from .play import ops as play_ops

    # Preferences FIRST: every operator below resolves the toolchain through them, and an
    # AddonPreferences that is not registered yet reads as "nothing configured".
    # field_widgets BEFORE ui: the Components panel draws those RNA slots.
    # component_ops BEFORE ui: the Components panel draws those operators, and a panel that
    # names an unregistered operator draws a dead button rather than failing loudly.
    for cls in (
        *prefs.classes, *ops.classes, *play_ops.classes, *field_widgets.classes,
        *component_ops.classes, *ui.classes, *browser.classes,
    ):
        bpy.utils.register_class(cls)
        _REGISTERED.append(cls)

    field_widgets.attach()

    # The Asset Browser's drop is Blender's own operation and cannot be replaced, only followed --
    # see paradise_assets.dropped for why that handler exists and how it stays harmless.
    dropped.register_handler()

    # Blender's own save writes the document too: the .blend is a disposable cache, so saving only
    # the cache is how a day's work goes missing. See paradise_assets.materialize.sync.
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

    # Reverse order: a child panel registered against a parent's bl_idname must go first, or
    # Blender warns about an unregistered parent while tearing the tab down.
    while _REGISTERED:
        bpy.utils.unregister_class(_REGISTERED.pop())
