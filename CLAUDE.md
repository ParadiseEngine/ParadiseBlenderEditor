# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

One Blender extension, `paradise_assets`. **`assets/` in the game repo is the source of truth**;
the `.blend` is a disposable cache of one `*.prefab`. The addon opens a prefab document,
materializes it as Blender objects, lets an author place things and edit components, and writes
the document back. The `paradise` CLI compiles `assets/` into `build/`; the game loads that.

| | |
|---|---|
| source of truth | `assets/` in the game repo |
| the other thing | the `.blend` under `.editor/blend/`, a cache of one document |
| format | `*.prefab`, canonical TOML |
| does | open a prefab document, place things, edit components, save it back, play it |

This repo shipped a **second** addon until recently: `paradise_blender`, in which the `.blend`
was the source of truth and `data/` was exported from it — a Python reimplementation of the C#
`Paradise.Export` contract. ShiningPie finished migrating off it, nothing else used it, and it
was removed along with its `.NET` bridge (#35). Two consequences worth knowing when reading old
commits or issues:

- There is **no .NET in this repo at all** any more, and no contract-conformance gate. That gate
  existed to catch a second implementation of the contract drifting from the first; the only
  implementation left is the engine's own.
- Anything describing an export to `data/scenes/*.json`, live preview, navmesh baking, KTX
  transcoding in Python, or entity identity derived from an object's name is describing that
  addon and does not apply here.

## Commands

```bash
./tools/run_tests.sh                           # everything
.venv/bin/python -m pytest tests/unit -q       # fast: document format, no Blender

blender --background --factory-startup --python tests/integration/test_axis_parity.py

python3 tools/install_addon.py                 # symlink into Blender's extensions dir
```

Integration tests that need a real asset project take one via `PARADISE_ASSETS_PROJECT` and skip
cleanly when it names nothing.

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

## Things that will bite you

**Read `CONVENTIONS.md` before touching transforms or numbers.** It documents the four places
Blender's conventions differ from the document's, and each one fails silently rather than loudly.

**Convert the matrix, then decompose.** Never decompose a Blender transform and convert
position/rotation/scale separately: the basis change permutes axes, so Blender scale `(1, 2, 3)`
is document scale `(1, 3, 2)`. `document/axes.py` is the only correct path, and
`tests/integration/test_axis_parity.py` is what pins it against Blender's own glTF exporter.

**`paradise_assets/__init__.py` must not import `bpy` at module scope**, and neither may
`document/` or `edits.py`. Python executes a package's `__init__` before any submodule, so a
top-level `import bpy` makes them unimportable outside Blender — which kills the unit tests, the
only defence keeping the canonical TOML writer byte-identical to the C# one. Blender-dependent
imports live inside `register()`.

**Blender handlers need `@bpy.app.handlers.persistent`.** Without it Blender drops them on file
load, and the failure is invisible: save-on-save and the watcher adoption simply stop working
after the author opens another `.blend`.

**A `register()` that raises must unwind itself.** Blender keeps whatever had already been
registered, and every enable after that dies on "already registered as a subclass" — the addon
cannot be turned back on without restarting Blender. `__init__.register` wraps the whole thing
and calls `unregister()` on failure.

**The canonical TOML writer is a CROSS-LANGUAGE contract, and it is checked by bytes.**
`document/canonical_toml.py` and C# `CanonicalTomlWriter` must produce identical output;
`paradise assets prefab-check` compares bytes, so a formatting difference is a failing CI check
on every document the addon has touched, not a style nit. Floats are specified as *Python's
`repr` rules* on purpose — the C# side adopted them so this side could be one call. Do not
"improve" the formatting.

**An object nobody moved must keep its authored numbers verbatim.** Documents store values that
came from C# `float`, Blender stores float32, and the axis rebase runs a square root — the round
trip is accurate to ~4e-8 relative, which is fine as a position and fatal as text, because
`repr` of a value that moved in its last bit is a completely different string. `save._unchanged`
is what keeps a one-object edit from rewriting every transform in the file. Its epsilon is not
tuning: below it, the load itself would churn the document.

**Normalize a document quaternion before composing it.** They are float32-quantized, so none is
exactly unit, and the length error leaks through the rotation matrix and comes back out of the
decompose as SCALE — it turned a stored `20.0` into `19.999998` on ShiningPie's skyline props.

**Reload and Recreate are not near-synonyms, and the panel must not let them read as a pair.**
`reload_prefab` re-reads the document into the CURRENT session, keeping the working file's
extras, camera and selection. `recreate_workfile` deletes `.editor/blend/<...>.blend` AND the
`.blend1` beside it — a backup of exactly the state being discarded is one File > Recover Last
Session from undoing the whole point — then rebuilds from the document. It parses the document
BEFORE deleting anything, so a document that will not parse leaves the author with the cache
they still had rather than with neither it nor a scene.

**A load leaves the scene holding the document and nothing else, but only when asked.**
`load_document(..., clear_startup=True)` removes Blender's startup content — the cube, the
camera, the light and the `Collection` around them — and only `open_prefab` passes it, because
that is the one path a PERSON takes into an otherwise empty Blender. It is opt-in rather than
automatic, and the reason is in the tree: `thumbnail.py` starts an empty file, links its own
camera and key light, then materializes. A gate that trusted `bpy.data.is_dirty` alone (which
follows undo pushes, so a script's edits do not set it) deleted that camera and rendered every
prefab black. Within the opt-in, `is_dirty` still protects a session someone has worked in.

The glTF importer's own leftovers are handled where they are made: `meshes.py` diffs
`bpy.data.collections` across the import the same way it diffs objects, and drops the ones its
move left empty — `glTF_not_exported` otherwise sits in the Outliner beside the library for the
life of the session.

**A GROUP is an Empty, and the format knows nothing about it.** A document object carrying only
`meta` and `transform` whose members are its children is shown exactly like every other object:
an Empty with Blender parenting, which the Outliner nests, drags, and moves as a unit. An earlier
design showed such objects as Blender Collections; it could not carry a transform (a Collection
cannot be moved), could not hang under an ordinary object (a Collection has no `parent`), and
had to re-derive group-ness from shape on every load, so emptying one flipped its representation.
Empties have none of those limits, and the group rules collapsed to one: the save adopts an
Empty the author made that holds at least one document object, minting it an identity and
hanging an unparented one off the document root (`save._adopt_new_groups`) — a second root
never loads. A stray camera or light, or an Empty with nothing in it, stays Blender's own.
Never over the root: with no unique parentless root nothing is adopted and the foreign-parent
rule names the Empty, or dragging the root under a new Empty would have minted a new root.

The one-gesture version is **Group Selected** (`materialize/grouping.py`, right-click in the
Outliner or viewport): a new Empty at the active member's position, hung where that member
hung, members re-parented in place. It parents with an IDENTITY `matrix_parent_inverse` and
rewrites the local channels, never through `parent_set`, because Blender keeps an object in
place across Ctrl+P and Outliner drag-to-parent by storing the offset in the inverse — which
the document has no field for. The save folds such an inverse into the channels once
(`save._fold_parent_inverses`) so hand parenting is safe too; before it, an object parented by
Ctrl+P loaded back somewhere else.

**A collision shape is an Empty under its object, and the Empty is the editor.** A collider's
list holds the game's rows, each with one member typed as the host shape (`authoredBy: shape`,
the six geometry members nested under it — ShiningPie's `Shape`) beside the game's own members
(`IsTrigger`). The HOST draws and bakes the nested member; the panel types the rest. A trigger
marker's `Volume` is the same row as ONE field rather than a list: one Empty, its row kept at the
field's own path (`Volume/IsTrigger`), the key removed when the Empty is deleted so the game
refuses the marker rather than sensing with nothing. `materialize/shapes.py` makes one child Empty per row on load
(box: CUBE display, scale = Size; sphere: SPHERE display of size Radius; capsule: a CUBE
envelope scaled `(2r, h, 2r)`, Y-aligned in the document so rotate the Empty to orient it) and
bakes each Empty's LOCAL transform back into its row on save (`document/collider_shapes.py`
holds the numbers, tested without Blender). Deleting the Empty deletes the row; the panel's
Box / Sphere / Capsule buttons add one; the row's game members (Id, IsTrigger, Layer…) stay
typed in the panel and land on the same row as the moved Empty. A shape Empty carries NO
document identity, so nothing that walks document objects sees it — except
`load._clear_previous`, which must remove them or a reload shows two documents' shapes. Shapes
are made for the components this DOCUMENT authors — for an instance, its own entry as the file
spells it, not the expansion that folds the prefab's components in: ShiningPie's props carry no
collider, every one of its 122 placed instances does.

**An instance loaded from a document carries no prefab reference of its own** — the expansion
in `resolve.py` replaces the instance entry with the prefab's resolved root, consuming it. So
`load.py` tags the object with `store.tag_prefab` as it materializes, which is the only reason
"open the prefab this came from" works for anything but an instance added in this session. The
tag is display data: `save.py` reads `store.prefab_of` only for an object the re-read document
has no entry for, so tagging an existing one changes nothing it writes.

**Components are passed through, never rebuilt.** `save.py` takes payloads from the RE-READ
document, not from Blender. That one decision is what lets a scene full of components this addon
has never heard of be opened and saved without corruption.

**Editing a field does not change that, and the shape of the edit is why.** `edits.py` holds an
OVERLAY — `{component id: {field: value}}`, only the members an author actually touched — applied
over the file's version at merge time. So a component nobody edited is still written byte-for-byte,
a field nobody edited keeps whatever the file says (including one this addon has no schema for),
and the overlay is cleared once the save has written it, so an old edit cannot resurrect itself
over a newer value. The ID property holding the payloads is still display data; do not write back
from it.

A field is offered as editable only when the GAME's schema describes it *and* nothing else
authors it: `[AuthoredByHost]` fields are shown locked, because their value comes from the object
they point at and typing one in would be authoring in the place the build overwrites. `meta` and
`transform` are refused by the vocabulary outright — Blender's name field and transform gizmo are
their editor, and a second way to type an identity is a second thing that can disagree.

`edits.py` imports no `bpy`, and that is load-bearing rather than tidy: it is the only new logic
on the save path, and being importable outside Blender is what lets it be unit-tested against a
plain dict. Keep it that way.

**There is ONE schema, and it is the game's.** A launcher built with
`ParadiseAuthoringScanReferences` merges every assembly it references into the document it dumps,
so the engine's components arrive inside `.editor/authoring-schema.json`, described by the engine
that game is actually built against. A vendored copy would not merely be redundant but a hazard —
merges are first-wins, so a checked-in copy that had drifted would win against the truth. A data
directory with no dumped schema therefore has **no components at all**, not just no game
components, so the panel says so loudly and the fix is "build the launcher".

**This addon does not mint identities — it writes a file and WAITS for one.** Identity lives
only in `<asset>.meta`, and `paradise assets watch` runs the C# `SidecarMaintainer`, which writes
a sidecar for any file under `assets/` lacking one. Two minters race and the loser's guid is
dropped with a `Conflicted` log line, so `document/sidecar.py` has `read` and `wait_for` and
deliberately no `write`. A creation therefore has a prerequisite rather than an ordering rule:
no watcher, no identity, no new prefab. `watch.ensure` starts one before anything
is created, and the extract operator refuses up front rather than after rewriting the level.

Batch the wait, don't repeat it. The watcher reconciles a set of new files together, so waiting
per file turns one reconcile into one timeout per file. `new_prefab.write` then `identify_all`
under a single deadline is the shape.

Two deliberate divergences from C# `SidecarMeta.Parse`, both because this side only READS: a
missing `schema_version` is accepted, and a stray root scalar (a legacy `kind = "document"`) is
ignored rather than refusing the document. C# refuses because its next rewrite would drop the
key; refusing here would cost the asset every reference to it — no catalogue entry, no pickable
reference. A *declared* version this build cannot read is still refused.

**A generated model prefab is a SEED, not a projection.** `paradise assets extract` writes one
beside a newly imported GLB so it is placeable straight away, and from that moment it is an
ordinary document its author owns: nothing records which model it came from, nothing updates it,
and nothing deletes it. The mirror that used to do all three, and the `meta.GeneratedFrom` marker
it needed, were removed (ParadiseEngine#256) — a model is one hop away through the mesh
document's `source` when anyone actually needs it.

**A rigged model gets a different component, and that is read from the model.** `document/gltf.py`
parses only the GLB's JSON chunk (never the geometry) and calls a non-empty `skins` array a rig.
`extract` reads the game's authoring schema for a mesh-bearing component and picks the skinned
one for a rigged model, because authoring a rigged model as static gives a prefab that loads,
shows the mesh, and is the wrong kind of thing in the game. It warns and writes a prefab with no
mesh rather than guessing when the schema names none.

**Extraction keeps the extracted object's identity on the INSTANCE, and gives the prefab root a
new one.** That is what the resolver does (`resolve.py`: the resolved root's guid IS the instance
guid), so every reference to the extracted object survives — and every reference to one of its
CHILDREN does not, because a child's resolved identity is `uuid5(instance, prefab-local)`.
`document/extract.py` warns per broken reference rather than refusing; the fix is an authoring
decision. The instance is left carrying `meta` and `transform` only: a copy of anything else
would be an override, and an override shadows the prefab forever, so editing the new prefab
would appear to do nothing at the one place it came from.

`ExtractResult.remaining` is a METHOD taking the new prefab's reference, not a field. The
reference does not exist until the watcher has identified the prefab, and making the level's new
version unobtainable without it is what stops a caller writing back a plain object where its
instance should be.

**Blender's name namespace is not the document's.** Blender uniquifies (`Wall` → `Wall.001`) and
truncates in one namespace shared with every imported GLB node, so `obj.name` alone cannot say
whether the AUTHOR renamed anything (#32). `store.tag_name` records both the authored name and
the one Blender showed; `store.document_name` compares them.

**Blender rejects empty enum identifiers.** An `EnumProperty` item with `""` as its identifier
warns "current value '0' matches no enum" and becomes unreadable. Use a `NONE` sentinel and map
it back where the format's value is `""`.

## When the document format changes

C# is the source of truth — `Paradise.Assets.Documents` for the prefab format and its canonical
writer, `Paradise.Export.Data.LevelDocument` for what a build compiles it into. On a change:

1. Update `document/prefab.py` and `document/well_known.py` to match, same field spellings.
2. Update `document/canonical_toml.py` only in lockstep with `CanonicalTomlWriter`, and refresh
   `tests/fixtures/parity/` from `Paradise.Assets.Documents.Test/Fixtures/parity/` — never by
   hand. A hand edit that still parses makes the test pin a form the writer does not produce.
3. Run `paradise assets prefab-check` over a real project. It compares bytes.

## Cross-repo boundary

This is an independent git repository, like its siblings in the workspace. **Never create a
commit spanning repos.** PRs are assigned to quabug; a PR that fixes an issue carries
`Closes #NNN` (one line per issue) at the top of its body and in the commit message so merging
closes it, with `Towards #NNN` only for deliberately partial work.

## Style

- Match the surrounding code: type hints, `from __future__ import annotations`.
- Code explains itself; comments explain why. Prefer a name, a type, a small function, or an
  assertion over a comment that says what the code does, and restructure before commenting.
  A comment or docstring is for what code cannot say: a Blender-API gotcha, a constraint, a
  decision and its rejected alternative, a failure mode someone would "fix" back in, a
  cross-language contract. Delete comments that narrate control flow or restate the next line;
  private helpers whose name says what they do get no docstring.
- Warnings to the author should say what will go wrong at runtime and how to fix it, not just
  what was skipped.
