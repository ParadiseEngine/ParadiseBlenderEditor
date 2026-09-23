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

## Authored actions

Components may declare C# authored actions in the game schema. Buttons, toggles and preview controls appear below
that component's fields, using its action names, labels and documentation. The editor saves
pending document edits before invoking a button or toggle. It then calls the generic
`paradise assets invoke-action` command; the game owns the behavior, generated paths, mesh
selection, baking and any other domain rules.

After a successful Save or Ctrl+S the addon invokes the component's save hooks: every action
declared `onSave`, including save-only methods of `kind: "save"` that draw no control — a
marked toggle is re-invoked with its stored value. The C# method decides which work its toggle
state enables. Manual invocation
suppresses this dispatch for its preliminary save so an action cannot recursively invoke itself.
Toggle state is keyed by document, entity and component in the disposable working file, never
written into a prefab payload. Reopening or reloading a document replays its enabled toggle
callbacks to restore their effects; it runs no save hooks. A failed restoration leaves that
toggle off.

Preview providers use `kind: "preview"` and return viewport geometry. Their eye controls keep
visibility in the working file, separately for each document, entity, component and provider.
Enabling a preview saves pending document edits and requests its geometry without passing a
toggle value to C#. Hiding it is immediate and local, including while a request is running.
Preview visibility never enters the business toggle state sent to authored actions.

Enabled providers refresh after successful actions and save hooks, including a Bake that only
changes a binary asset. Reload restores enabled previews. Refresh requests are deduplicated,
and providers with the same overlay id remain independent. Removing a provider, component or
entity clears its preview; late results cannot restore a hidden or removed preview. A failed
or stale result clears that provider's geometry while retaining the visibility preference so
the next save can retry. Newer local edits always survive a pending request.

Actions run asynchronously in interactive Blender. The component shows progress and failures,
and another document save waits until the action finishes. Triangle overlays returned by an
action render in the viewport without adding scene objects. Reloading a document or unloading
the addon cancels pending jobs and clears the overlays. If an action changes the canonical
document, the editor refreshes its view only when no local edits were made during the job;
otherwise it preserves those edits and reports that the changes need reconciliation.

Scene navigation is one client of this mechanism: the game declares Bake, Preview and Auto-bake
on Save, and a locked `NavMeshFile` field. The editor has no navigation-specific operators or
geometry extraction. Use a CLI and rebuilt game schema that support authored actions; the
published CLI predating these commands cannot invoke them.
