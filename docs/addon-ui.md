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
  project_settings.py  game-declared project TOML documents, edited without an active prefab
  edits.py      ★ the component-edit overlay — no bpy, unit-tested against a plain dict
  clip_ops.py     the Components panel's clip operators — root-motion flag + root-bone pick
  browser.py      the Asset Browser context menu
  context_menu.py the Outliner's and viewport's object context menus
  catalogue.py    the Asset Browser library and its thumbnails
  watch.py        `paradise assets watch` as a per-project background process
tools/
  install_addon.py   symlink the package into Blender's extensions directory
  run_tests.sh       unit + integration
```

## The panel

Sibling panels in the **Paradise** tab, one per scope:

| panel | poll | scope |
|---|---|---|
| Prefab Document | always | the open document; a landing state when there is none |
| Project | a project is locatable | the watcher, Build / Verify / Clean, the catalogue |
| Project Settings | a project is locatable | game configuration, catalogs, renderer and tool settings |
| Play | a document is open | running the game on it |
| Document Tree | a document is open | document hierarchy and pending overrides |
| Components | a document is open and something is selected | the active object |

They are siblings because **project actions do not need a document**. `store.project_of` finds
the project from the open document, else from `bpy.data.filepath` — a workfile under
`.editor/blend/` is already inside its project. Nesting Build and the watcher under the document
panel made them unreachable in the one session that most needs them: the one that just started.

## Project settings

**Project Settings > Choose Settings Document** lists the documents declared by the game build
in `.editor/authoring-documents.json`. Each uses the same typed widgets as components, including
nested lists, enums, asset references, and optional fields. **Set** creates an optional value;
**Clear** omits its TOML key. Missing ordinary values show the game's defaults without adding
them to the file. List edits can be reverted at the containing list when its structure changed.

Changes remain pending until **Save Settings**. **Reload Settings** discards pending changes
and reloads disk values. Save retains unknown and unrelated current values; it refuses if an
edited value or its containing indexed list changed on disk, so a reordered row cannot receive
another row's edit. Switching projects retains pending work; return to that project to save or
use **Discard Pending Settings** before choosing a document in the new project. Settings save
is explicit and independent of Ctrl+S for the prefab workfile.

**Add Component** uses only declarations in the current game schema and excludes component
types bound to project documents. Existing unknown component payloads remain inspectable and
survive saves; they do not become extra Add Component choices.

Optional component fields also have **Set** and **Clear** controls. Clear omits an owned field
when the prefab is saved. An inherited top-level field cannot be omitted by an instance override;
edit its source prefab to clear it, or use **Revert to Prefab** to restore the inherited value.

The Components panel gains an **Animation clips** section when the selected object's mesh
resolves to a GLB that carries animations (`document/glb_clips.py` reads the GLB's JSON
chunk). Each clip row is a root-motion toggle and a root-bone picker; both write the GLB's
`.meta` sidecar immediately — the `[glb].clips` domain, keyed by glTF animation index —
because the setting belongs to the model, not to the open document, and the `.blend` is
disposable. A static mesh draws nothing at all.

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

## Transform placement fields

A component field marked `authoredBy: transform` uses a placement handle. **Select Handle**
selects its arrow Empty for ordinary move/rotate/scale editing. **Create Handle** assigns a
previously empty field at the component owner's placement. **Pick Object** copies another
object's current world placement into the handle; later edits to that source object do not
change the copied destination. **Clear**, or deleting the handle, unassigns the field.

Selecting a handle keeps its owner's Components panel visible. An inherited placement can be
edited on a prefab instance or a directly resolved child; only that field becomes an override.
**Revert to Prefab** restores the inherited placement when the document is saved. A child from
a nested prefab has an inspection-only handle: Select remains available, but changes to that
handle do not write an override or prevent saving other edits. Saving restores its authored
placement, including recreating a deleted handle. To change the field, open the prefab that
authors the component.

The document object's own `transform` component remains edited through Blender's transform
gizmo. These handles edit nested component fields such as a transport's `Destination`.

Integer fields whose declared range or current value exceeds Blender's signed 32-bit spinner
use a decimal text field. This includes unsigned renderer seeds through `4294967295`. Edits
remain integers in the saved document. Invalid text or values outside the declared range show
an error and restore the last accepted value; ordinary integer fields keep their spinners.

## Scene navigation

A string field marked `authoredBy: navmesh` displays a read-only path and navigation controls.
The path always follows the open level: `assets/levels/battlefield.prefab` produces
`assets/levels/battlefield.navmesh`, referenced as `levels/battlefield.navmesh`. Saving updates
that generated field while preserving the rest of the component payload.

- **Bake** saves the current level, then runs the CLI's Recast bake on evaluated static document
  mesh instances. World placement, modifiers, and mirrored winding are included. Animated or
  skinned objects and dynamic/kinematic rigid bodies are excluded. A game schema can mark a
  boolean field `authoredBy: navmesh-geometry`; false excludes that object and its descendants.
  ShiningPie's cars default to excluded, with an opt-in for stationary scenery.
- **Preview** shows or hides the baked walkable triangles as a green viewport overlay. It can
  read an existing `.navmesh` without rebaking. Reloading or switching documents clears the
  overlay; no preview objects or preview state are written into the level.
- **Auto-bake on Save** is off by default. When enabled, both Save and Ctrl+S bake after a
  successful document save. The setting belongs to this level's working `.blend`. Repeated
  saves queue the latest snapshot while the current bake finishes. A failed bake retains the
  previous binary and shows the error in Scene navigation; the document save still succeeds.

Baking runs in the background in interactive Blender. Disabling the addon or loading another
document cancels and cleans up its pending work. The CLI owns `.meta` creation through its
normal asset watcher; the addon only writes the baked binary. Build assets to copy it into the
game's runtime `build/` tree.

These controls require a CLI with `assets bake-navmesh` and `assets preview-navmesh`. During
local development, build `ParadiseEngine/src/Paradise.Cli/Paradise.Cli.csproj` and select that
project in the addon's **Paradise CLI** preference. Published CLI 0.51.0 predates these commands
and the `.navmesh` importer.
