# Addon layout and UI

Paths in this reference are relative to the repository root.

## Layout

```
paradise_assets/
  document/     ★ pure Python, imports no bpy — the *.prefab format, the canonical TOML writer,
                  the axis rebase, sidecar reading, the game's component schema
  materialize/    document <-> Blender objects: load, save, mesh instancing, ID-property store,
                  the working .blend, save-on-save
  play/           the CLI: resolution, Build / Verify / Clean / Play, the running session
  ops.py          open_prefab / save_prefab / reload_prefab, add_prefab_instance,
                  extract_prefab, toggle_watch, refresh_catalogue
  ui.py           the Paradise sidebar tab
  edits.py      ★ the component-edit overlay — no bpy, unit-tested against a plain dict
  browser.py      the Asset Browser context menu
  context_menu.py the Outliner's and viewport's object context menus
  catalogue.py    the Asset Browser library and its thumbnails
  watch.py        `paradise assets watch` as a per-project background process
tools/
  install_addon.py   symlink the package into Blender's extensions directory
  run_tests.sh       unit + integration
```

## The panel

Four SIBLING panels in the **Paradise** tab, one per scope, not one tree:

| panel | poll | scope |
|---|---|---|
| Prefab Document | always | the open document; a landing state when there is none |
| Project | a project is locatable | the watcher, Build / Verify / Clean, the catalogue |
| Play | a document is open | running the game on it |
| Components | a document is open and something is selected | the active object |

They are siblings because **project actions do not need a document**. `store.project_of` finds
the project from the open document, else from `bpy.data.filepath` — a workfile under
`.editor/blend/` is already inside its project. Nesting Build and the watcher under the document
panel made them unreachable in the one session that most needs them: the one that just started.

Two entries also hang off the object context menus (`context_menu.py`) — the Outliner's and the
viewport's, one `_draw` for both — gated on the active object being a DOCUMENT object, since a
menu that grows two greyed rows on every cube is worse than one that says nothing. "Open Prefab
in New Blender" starts a second Blender rather than replacing the session: a level and the prop
it instances are two documents, and making people close one to edit the other is what stops them
editing it.

Two rules for anything drawn here:

- **A draw runs at redraw rate.** Nothing in it may log (a warning would fire per frame), walk
  the asset tree (use `ui._cached`, 2 s TTL), or write an ID property (Blender forbids it — that
  is why new schema fields appear behind a sync button).
- **Say a problem once.** `play.ops.tool_problems` (machine: CLI, ktx) is the Project panel's;
  `play.ops.play_problems` (this project: `[host]`) is Play's. Reporting a missing CLI in both
  reads as two faults.
