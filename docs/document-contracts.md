# Blender and document contracts

Use the section relevant to the change. Paths in this reference are relative to the repository
root. [CONVENTIONS.md](../CONVENTIONS.md) defines coordinate conversion, numeric formatting,
identity and names.

## Coordinates and round trips

**Convert the matrix, then decompose.** Never decompose a Blender transform and convert
position/rotation/scale separately: the basis change permutes axes, so Blender scale `(1, 2, 3)`
is document scale `(1, 3, 2)`. `document/axes.py` is the only correct path, and
`tests/integration/test_axis_parity.py` is what pins it against Blender's own glTF exporter.

## Addon lifecycle

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

**Authored actions belong to C#.** `document/actions.py` reads the generic action protocol;
`action_ops.py` presents schema-declared buttons, toggles and preview providers and invokes the CLI. A save
dispatches every `kind: "save"` action — a marked button or toggle carries a second save entry
under its method name — plus the older `onSave` spelling, passing the component's toggle state. Domain choices
such as geometry selection, output names and whether to bake live in the C# callback. Manual
invocation saves edits with save-action dispatch suppressed to avoid recursion.

Action jobs serialize per scene. A document-changing response may rematerialize the scene only
if its live-object fingerprint still matches the job's starting state; otherwise the author’s
new edits stay intact and the stale document is reported. Viewport overlays are keyed by their
document/entity/component owner and overlay id, and never become scene objects. Reload cancels
jobs, clears overlays and replays enabled toggle callbacks. A refresh caused by an action skips
that replay, preventing recursive invocation. Unregister closes child processes and removes
both timers and draw handlers.

`kind: "preview"` has a separate editor-owned lifecycle. Visibility lives under
`paradise_action_previews`, keyed by document/entity/component/provider, and never enters the
business `ToggleValues` transport. Enable saves canonical edits before invoking the provider
without `--value` or `--on-save`; disable only hides locally. Provider overlays add the provider
name to their ownership, so matching overlay ids do not collide with each other or ordinary
action overlays. Every provider response replaces its previous geometry. Enabled providers
refresh after successful actions and save hooks, even when `documentChanged` is false. Queued
refreshes deduplicate per provider and run after pending business actions; preview responses do
not trigger further refreshes. A response must still match the live document stamp, object
fingerprint, declared provider and visibility generation before it can display. Failure clears
stale geometry but retains the visibility preference for a later refresh. Component, entity
and provider removal prune visibility and overlays, while reload cancels jobs and restores the
remaining enabled providers.

## Canonical serialization

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

## Session and hierarchy editing

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

**A collision shape is an Empty under its object, and the Empty is the editor.** A shape row is
the host shape itself (`authoredBy: shape` on the row — ShiningPie's `AuthoredColliders` rows,
and every marker's `Volume`: `ObstacleMarker`'s solid, a trigger's sensor) or a game record with
one member typed as the host shape beside the game's own members. The HOST draws and bakes the
geometry; the panel types whatever else the row carries, which for a bare shape is nothing. A trigger
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

## Prefab overrides and save state

**An instance's overrides are authored as CARRIERS, and the trap is the baseline you measure
against.** Moving a prefab's child, or editing a field on an instance, writes an override rather
than a copy: the instance's own entry gains a component holding ONLY the touched fields, and a
child's change goes on a `meta.Target` carrier keyed `(instance guid, prefab-local guid)` -- the
same key `resolve._expand_document` refuses duplicates on, which is what makes "update, don't
duplicate" a lookup. Three rules paid for here:

- **A carrier's transform is a PARTIAL.** `resolve._merge_data` merges per field, so a carrier
  holding only `Position` inherits rotation and scale from the prefab. Measuring a move against
  that partial reads the absent keys as the IDENTITY and calls every untouched child moved, which
  rewrites all three channels on every save of a file nobody edited -- and `prefab-check` compares
  bytes. Move detection is against the RESOLVED snapshot (`store.component_json`, what the load
  displayed); pruning an override that has come back is against the PREFAB baseline
  (`store.base_json`). One key cannot be both.
- **"The author deleted this child" must not be inferred by re-reading the prefab at save time.**
  One transiently unreadable prefab would then write `Dropped = true` across a level, from a save
  that reported success. The load records what it actually materialized (`store.tag_children`),
  and only a local guid that was SHOWN can be dropped.
- **A carrier that says nothing is pruned, but `Dropped` is content.** Pruning a dropping carrier
  resurrects the child on the next load.

**A prefab's child can be moved but not re-parented, and not half-deleted.** `resolve._rewrite_meta`
takes a resolved child's parent from the PREFAB's topology and ignores the carrier's `meta.Parent`
(which addresses the instance), so there is no field to write a re-parent into -- refused by name.
Blender's Delete orphans children rather than removing them, so deleting a child mid-tree is
refused too; deleting the whole INSTANCE is ordinary, and its display objects are swept rather
than refused (`save._drop_widowed_derived`).

**The load and the save tag through ONE function** (`materialize/tagging.py`). They used to
disagree: `_refresh_snapshots` rewrote an instance's payload from the LEVEL entry -- meta,
transform and overrides -- over the RESOLVED payload the load had put there, so saving a level
collapsed every instance's Components panel to two rows until the next reload. Both callers now
resolve and tag through the same code, so that disagreement is not a thing that can be written.

**Outliner marks are display, and there is exactly one writer.** Blender exposes no per-object
Outliner icon, so `Crate \u25b8` / `Bulb \u00b7` / a trailing `*` is the only channel there is.
`store.mark` sets the name and records it in the same call, because `document_name` trusts
`SHOWN_NAME_KEY` to tell an author's rename from Blender's uniquifying (#32) -- a caller that set
`obj.name` and forgot to record it would write `Crate \u25b8` into `assets/`. The marks track the
DOCUMENT, not the edit overlay: they are written from the load and the save, never from a draw
(Blender forbids writing an ID property there), so a pending override shows in the Document Tree
panel, which reads live state, and reaches the name on the next save.

**An instance may not author a collider its prefab declares.** A shape row is a list entry and
`_merge_data` is shallow, so overriding one replaces the WHOLE list and silently drops the
prefab's other rows -- bought with a viewport drag, the cheapest gesture for the most expensive
edit. `shapes.editable` keeps refusing it. If it is ever wanted, the shape is an explicit
"override colliders on this instance" button that copies the list down ONCE, so the shadowing was
asked for.

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

## Editable meshes

**An object that owns its geometry is recorded in the GLB, not in the document.** Make Mesh
Editable (`ops.py`) copies the geometry an object shows into `<document folder>/<document
stem>/<Name>_<guid8>.glb`, waits for the watcher to mint its `.mesh` document (found through the
GLB sidecar's `[extract]` record), unpacks the instance if it was one, and points the object's
mesh field at that document. The GLB's `scenes[scene].extras.paradise_mesh_owner` holds the
owner's guid: the load builds a real, editable Blender mesh for that object alone
(`document/editable_mesh.owns`), and anything else referencing the same `.mesh` sees an ordinary
instance. No document field exists for it, so the engine reads the level exactly as before and
`paradise assets mv` cannot break the link.

**Materials bind by position, so slot order IS the contract.** Engine `MeshBlob` binds `Slots[i]`
to glTF primitive `i`, counted in `GltfSceneReader.BakeInstances` order: depth-first from the
default scene's roots, children as each node lists them. `document/editable_mesh.mesh_instances`
is that walk, and `materialize/editable_mesh.build_mesh` rebuilds a GLB as one mesh with a slot
per primitive in that order. Blender's importer cannot do it: it merges primitives that share a
material, and names objects after nodes that ShiningPie's multi-part models reuse by the dozen,
so neither slots nor parts could be matched afterwards. Slot materials in Blender are display
only (`Paradise/<material document>`), assigned from `Slots` on load and after every save.

**The GLB is written with placeholder materials.** `export_materials='PLACEHOLDER'` keeps one
primitive per used slot and writes no `materials` array, and `AssetExtractor.HasAuthoredParts`
is exactly "materials or embedded images" -- so `verify` does not ask for an `extract` that would
mint a seed prefab and a copy of every material per placement. The exporter forces a `.glb`
extension onto its path, so it writes into a private temp directory; the result is copied to a
`.tmp` beside the target (in the default `[assets] ignore`) and renamed into place.

**The save exports what changed, refuses what would misbind, and never overwrites someone else.**
`store.EDITABLE_KEY` records per owned object the GLB (assets-relative), the SHA-256 of the bytes
last read or written, a fingerprint of the evaluated geometry (topology, every non-internal
attribute, corner normals, slot count) and the slot count. `editable_mesh.publish` runs after
every document check and before the document is written. An unchanged fingerprint exports
nothing, so an untouched scene writes zero bytes. A changed one is refused when the GLB's bytes
moved on disk, when the slots no longer show `Slots` in order, or when a slot lost all its faces
(the exporter drops that slot's primitive, shifting every binding after it). Every export is
staged before any GLB is renamed into place, so a refusal writes nothing.

**A reload keeps the author's object while its GLB is unchanged.** `load_document` takes owned
objects out of the scene before clearing it (`editable_mesh.stash`) and hands each back when the
GLB's bytes are still the recorded ones, so quads, modifiers and anything else a GLB cannot hold
survive. Otherwise -- a fresh clone, Recreate, someone else's change -- the object is rebuilt from
the GLB, triangulated with modifiers applied. That asymmetry is the price of the GLB being the
only truth.

**The watcher mints; the operator waits for it.** The `.mesh` document follows the GLB's own
sidecar out of `paradise assets watch`. The operator waits `MESH_WAIT_SECONDS`, and on a timeout
leaves the GLB in place: a watcher in play mode rebuilds after every change and queues new files
behind that build (minutes on a cold cache), and running the operator again reuses the object's
own GLB (`plan_target` accepts a file this object owns).

## Schema, identity and extraction

**There is ONE schema, and it is the game's.** A launcher built with
`ParadiseAuthoringScanReferences` merges every assembly it references into the document it dumps,
so the engine's components arrive inside `.editor/authoring-schema.json`, described by the engine
that game is actually built against. A vendored copy would not merely be redundant but a hazard —
merges are first-wins, so a checked-in copy that had drifted would win against the truth. A data
directory with no dumped schema therefore has **no components at all**, not just no game
components, so the panel says so loudly and the fix is "build the launcher".

The game may also emit `.editor/authoring-documents.json` (version 1), containing `documents`
with unique `id`, `displayName`, and an assets-relative `.toml` `path`. Each declaration has
either recursive `fields` for a plain root document or `componentId` to edit the `Data` of
exactly one matching `Components` entry using the existing component vocabulary. These are
bindings to game-owned types, not addon-maintained setting lists. Component types bound to
documents are excluded from entity Add Component. No undeclared fallback component is addable.

Project documents use a separate WindowManager edit overlay and RNA widget collection, so
they need no open prefab or active object. Optional fields use explicit Set/Clear and serialize
absence by omitting the key. Save rereads disk, checks the touched paths against the loaded
baseline, and merges only pending changes. Any changed containing array refuses indexed edits
because array indices do not provide stable row identities. Unknown and unrelated values are
preserved. Recursive schema array paths retain nested table headers while asset references
stay inline. No-op saves preserve original bytes; changed saves use canonical TOML and remove
comments, as other canonical writers do. Project settings are saved explicitly, independently
of the prefab's save-on-save handler.

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

The one sidecar domain the addon WRITES is `[glb].clips` — the per-clip root-motion settings
(`document/glb_clips.py`, ParadiseBlenderEditor#47). It still mints nothing: a write requires
the sidecar to already carry a guid, merges into whatever `[glb]` and sibling domains the
pipeline wrote, and leaves a file that only changed in bytes it meant to change. Entries are
keyed by glTF animation index because that is the slot the engine reads; the recorded `name`
is a witness that re-keys a setting when the clip moved and warns when it was renamed, not a
second key.

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

## World-placement handles

`materialize/transform_helpers.py` materializes `authoredBy: transform` component fields as
arrow Empties. A helper has no document GUID. Its tag identifies its owner, component and field;
the owner records which slots had helpers so deletion is distinguishable from an untouched
absent field. Unassigned payloads, including a zero `Scale`, stay unchanged until explicitly
assigned. Unknown members survive placement edits.

These fields store **world** placement. Evaluate document parenting before creating helpers,
then convert the complete matrix with `document/axes.py` before decomposition. A parent inverse
cancels the owner's initial matrix so nonuniform parent scale does not distort a newly loaded
placement. Save compares each handle to its displayed baseline and retains unchanged authored
numbers. It does not bake untouched inherited fields into new overrides.

A moved inherited helper writes only its top-level field to the instance or override carrier.
Clearing an inherited field writes an empty table, which overrides it as unassigned; omitting
the field would inherit the old destination again. Reverting a field skips the helper bake and
refreshes its placement after the successful save. Nested-prefab child edits are refused.

Reload sweeps helpers alongside document objects and collider handles. Group adoption excludes
helpers; parenting a document object under one is an error, never a new authored group.
`transform_ops.py` provides explicit create/select/clear/object-copy actions. Its panel draw
only reads existing state. Native coverage is `tests/integration/test_transform_helpers.py`.
