# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

**Two** Blender addons, which point in opposite directions. Know which one you are in before
changing anything — they disagree about what the source of truth is, and that is the whole
difference between them.

| | `paradise_blender` | `paradise_assets` |
|---|---|---|
| source of truth | the `.blend` | `assets/` in the game repo |
| the other thing | `data/`, exported | the `.blend`, a disposable cache of one scene |
| format | the JSON export contract | `*.prefab`, canonical TOML |
| does | author, export, play, live-preview | open a prefab document, place things, save it back |

`paradise_blender` is the older and much larger of the two: the **second** implementation of a
contract whose reference implementation is C# `Paradise.Export` (used by `ParadiseGodotEditor`).
That framing matters for almost every decision in it: when this repo and the contract disagree,
the contract is right.

`paradise_assets` is the inversion (the asset-management plan's §2.7). It reads `assets/` — the
committed source tree the `paradise-assets` CLI compiles — and writes only prefab documents. Both
can be installed and enabled at once, which is what made the migration possible. ShiningPie has
finished that migration and no longer exports through `paradise_blender`; what that addon is for
now (deprecated, or the JSON exporter for games without an `assets/` tree) is undecided — #35.

## Commands

```bash
./tools/run_tests.sh                          # everything
.venv/bin/python -m pytest tests/unit -q       # fast: contract math, no Blender

blender --background --factory-startup --python tests/integration/test_axis_parity.py
blender --background --factory-startup --python tests/integration/test_export_scene.py
blender --background --factory-startup --python tests/integration/test_live_preview.py

dotnet build tools/ParadiseBlenderBridge
dotnet run --project tools/ParadiseBlenderBridge -- contract-check <document.json> --verbose

python3 tools/install_addon.py                # symlink into Blender's extensions dir
```

The .NET bridge builds against the published `Paradise.Export` package by default. To compile
it against the engine sources in this workspace: `dotnet build -p:ParadiseUseEngineSource=true`.

## Layout

```
paradise_blender/
  contract/     ★ pure Python, imports no bpy — the contract implementation
  authoring/      property groups: how a scene declares what to export
  export/         scene walk → contract documents
  pipeline/       KTX2 transcoding, .NET bridge invocation, artifact cache, data/ cleanup
  play/           runtime resolution and detached launch
  live/           live-preview protocol, transport, session, sync
  ui/             the Paradise sidebar tab
paradise_assets/
  document/     ★ pure Python, imports no bpy — the *.prefab format and the canonical TOML writer
  materialize/    document <-> Blender objects: load, save, mesh instancing, ID-property store
  ops.py          open_prefab / save_prefab / reload_prefab, add_prefab_instance, refresh_catalogue
  ui.py           the Paradise Assets sidebar tab
tools/
  ParadiseBlenderBridge/   .NET CLI: navmesh bake + contract conformance check
  mock_runtime.py          reference live-preview listener (the protocol's executable spec)
```

## Things that will bite you

**Read `CONVENTIONS.md` before touching transforms, colours, or numbers.** It documents the
four places Blender's conventions differ from the contract's, and each one fails silently
rather than loudly.

**`paradise_blender/__init__.py` must not import `bpy` at module scope.** Python executes a
package's `__init__` before any submodule, so a top-level `import bpy` makes
`paradise_blender.contract` unimportable outside Blender — which kills the unit tests, the main
defence against contract drift. Blender-dependent imports live inside `register()`.

**Convert the matrix, then decompose.** Never decompose a Blender transform and convert
position/rotation/scale separately: the basis change permutes axes, so Blender scale
`(1, 2, 3)` is contract scale `(1, 3, 2)`. `export/transform.py` is the only correct path.

**Blender colours are already linear.** Do not call `srgb_to_linear` on a socket
`default_value` — that is the Godot host's job, not this one's. The exception is values
authored through widgets documented as sRGB (recipe tints, fog); those call sites say so.

**Blender handlers need `@bpy.app.handlers.persistent`.** Without it Blender drops them on file
load, and the failure is invisible: export-on-save and the schema watcher simply stop working
after the user opens another .blend.

**Never block the main thread in `live/`.** Blender's UI is single-threaded and the peer is a
game runtime that stalls for frames at a time. Sends go through a bounded queue drained by a
background thread.

**An export reuses unchanged artifacts, and the rule for what may be cached is strict.**
`pipeline/cache.py` stores KTX2 transcodes and navmesh bakes under `<project>/.editor/cache/`
(the engine's `ArtifactCache` directory, digest and layout, so either tool's artifacts serve the other),
keyed on a digest of the step's *complete* input — image bytes plus the encode's argv, or the
geometry-and-settings payload plus the bridge's build output. On ShiningPie that takes a
re-export from 44 s to 3.6 s. **Do not extend this to the mesh GLBs.** Their inputs are a
transitive closure over the depsgraph (modifier stacks, geometry nodes, materials, armature
actions, the exporter's own argv); a key that misses one does not fail, it ships last week's
asset and reports success — and the only hash that would not miss costs as much as the 3.9 s of
exporting it would skip. Reach for the panel's rebuild button (`paradise.export_scene(force=True)`)
after changing the exporter itself, since no input hash can see that.

**An export can also DELETE, and the rules that keep that safe are not optional.**
`pipeline/prune.py` removes artifacts no exported scene references any more — what a renamed mesh
leaves behind. It is **off by default** (`paradise_project.prune_data`) and stays off for a scene
with no property group attached: it is the only destructive step in an export, so it is a
deliberate per-project choice rather than something an addon update switches on under an author
who never asked for it.

When it does run, five rules keep it safe and none is decoration:

- Only the directories the exporter **writes** and only the extensions it writes there (`OWNED`) —
  so `data/audio` (Wwise), the game's own config, and an author's stray file are never candidates.
  Note `primitives/` is *not* owned: it is in the shared layout (`paths.py`) because the **Godot**
  host generates it, and owning what you cannot regenerate is how a cleanup loses an asset.
- **Every** `scenes/*.json` is a root, so a `data/` shared by two .blends is safe.
- Reachability is computed from string *values* rather than known keys, because a key whitelist
  goes stale the day a component gains an asset field, and the cost of going stale is deleting a
  live asset. Documents are followed transitively (scene → material → texture).
- A `.ktx2` with a source image beside it is kept, because `ktx.convert_data_directory` transcodes
  `sprites/x.png` → `sprites/x.ktx2` in place and the `.png` is not ours — deleting one half of
  that pair cleans nothing up and would eat a spritesheet in the window between dropping it in and
  wiring it to an entity.
- It refuses to run at all on an unreadable scene document, or when no scene declares any entity —
  that second one is an export that found nothing, against which the whole directory looks
  unreachable.

**Never restore an object's transform by assigning `matrix_world`.** The assignment decomposes
into location/rotation/scale, and the rotation half of that round trip is lossy at ~1e-6, so
save/restore leaves the object microns from where it started — 25 of ShiningPie's 321 objects
moved on every export, which churned the exported transforms and defeated any content-keyed
reuse. `export/mesh.py:_capture_transform` saves the channels instead.

**The entity property group holds HOST data only.** `ParadiseEntityProperties` is
deliberately four members (`is_entity`, `model_path`, the two collider lists);
every component — the engine's identity/agent/rigidbody/audio/particles included — is authored
through the schema in the Components panel and routed to its typed slot by
`contract/authoring_router.py`. Do not add fixed fields back: the ~40-field mirror this
replaced is exactly the thing a schema change silently drifts away from.

**There is ONE schema, and it is the game's.** This host used to vendor a copy of the engine's
own schema and merge it underneath — it can no longer read the C# constant, and the game's dump
described only the game. It does not have to any more: a launcher built with
`ParadiseAuthoringScanReferences` merges every assembly it references into the document it dumps,
so the engine's components arrive inside `<data>/authoring-schema.json`, described by the engine
that game is actually built against. The vendored copy was not merely redundant but a hazard —
merges are first-wins, so a checked-in copy that had drifted would have won against the truth.

Two consequences. A data directory with no dumped schema now has **no components at all**, not
just no game components, so the panel says so loudly and the fix is "build the launcher". And
`component_ids.engine_type_name` reads the CLR name out of that same document at export — see
`check_engine_ids`, which replaced the unit test that used to police the vendored copy.

**Authored components live in ID properties, not a PropertyGroup.** The game's own components
(`<data>/authoring-schema.json` → `Components.Custom`) are schema-driven data that changes on
every game rebuild, and property-group fields are class-level and registered once. So
`authoring/authored_components.py` stores them as per-object ID properties
(`obj["paradise:<id>/<Field/Path>"]`) and the panel draws them from the schema at draw time.
A LIST extends the same key grammar with an index segment (`Tables/0/Entries/1/Weight`), plus one
`#`-suffixed sentinel per list holding its row count (`Tables#`, `Tables/0/Entries#`) — the schema
says a member *is* a list and can say nothing about how long it is, so the count is data. That is
why `contract/authoring.py`'s `outline()` takes counts and `flatten()` is a facade over it, and why
adding, removing or reordering a row is a key-renumbering operation (`config_store._rewrite_rows`)
rather than a list mutation. The one-character suffix is not style: a key is
`prefix + 22-char token + "/" + path` under Blender's 63-character cap, and `/__count` would spend
eight of the ~36 characters a path has to live in. Editing rows is CONFIG-DOCUMENT only for now;
an entity's key budget is five characters tighter and exporting rows would change every scene.
Two consequences: never write ID data inside a `draw()` (Blender forbids it — that is why new
schema fields appear behind a sync button), and the wire format is pinned by the Godot host's
`AuthoredEntityCore.ValueOf`, mirrored in `contract/authoring.py` and its unit tests.
Colliders are the one component whose body is OBJECT REFERENCES rather than form fields
(`HOST_LIST_IDS`): the panel draws the entity's pointer collections as the Collider /
Interactable components, presence is "marker or a non-empty list" (build scripts fill lists
without markers), and removing the component clears the references — while renderable and
light stay read-only derived rows.

**Blender rejects empty enum identifiers.** An `EnumProperty` item with `""` as its identifier
warns "current value '0' matches no enum" and becomes unreadable. Where the contract's value is
`""` (e.g. `MaterialKind`), use a `NONE` sentinel and map it back at export — see
`authoring/material_props.py`.

## Things that will bite you in `paradise_assets`

**The canonical TOML writer is a CROSS-LANGUAGE contract, and it is checked by bytes.**
`document/canonical_toml.py` and C# `CanonicalTomlWriter` must produce identical output;
`paradise assets prefab-check` compares bytes, so a formatting difference is a failing CI check
on every document the addon has touched, not a style nit. Floats are specified as *Python's `repr`
rules* on purpose — the C# side adopted them so this side could be one call. Do not "improve"
the formatting.

**An object nobody moved must keep its authored numbers verbatim.** Documents store values that
came from C# `float`, Blender stores float32, and the axis rebase runs a square root — the round
trip is accurate to ~4e-8 relative, which is fine as a position and fatal as text, because
`repr` of a value that moved in its last bit is a completely different string. `save._unchanged`
is what keeps a one-object edit from rewriting every transform in the file. Its epsilon is not
tuning: below it, the load itself would churn the document.

**Normalize a document quaternion before composing it.** They are float32-quantized, so none is
exactly unit, and the length error leaks through the rotation matrix and comes back out of the
decompose as SCALE — it turned a stored `20.0` into `19.999998` on ShiningPie's skyline props.

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
they point at and typing one in would be authoring in the place the export overwrites. `meta` and
`transform` are refused by the vocabulary outright — Blender's name field and transform gizmo are
their editor, and a second way to type an identity is a second thing that can disagree.

`edits.py` imports no `bpy`, and that is load-bearing rather than tidy: it is the only new logic
on the save path, and being importable outside Blender is what lets it be unit-tested against a
plain dict. Keep it that way.

**`document/` must not import `bpy`** — same rule and same reason as `paradise_blender`'s
`contract/`: the unit tests are the only defence against the writer drifting from the C# one.

## When the contract changes

The engine's `Paradise.Export.Data.LevelDocument` is the source of truth. On a schema change:

1. Update `contract/schema.py` — same field order (System.Text.Json writes declaration order)
   and the same defaults.
2. Bump `SCHEMA_VERSION` only in lockstep with `LevelData.CurrentSchemaVersion`.
3. Run `contract-check` against a real export. It round-trips the document through the engine's
   own reader and writer and reports anything the reader dropped or the writer added — which is
   exactly what a missed field looks like.
4. Update `docs/parity.md` if the feature matrix moved.

Note the contract is **value-based, not byte-based** (the engine's own CONVENTIONS.md says so),
so `contract-check` compares semantically. Byte-identical output is a bonus that
`writer.f32_repr` currently achieves; do not break it casually, but do not treat a formatting
difference as a correctness failure either.

## Cross-repo boundary

This is an independent git repository, like its siblings in the workspace. **Never create a
commit spanning repos.** PRs are assigned to quabug; a PR that fixes an issue carries
`Closes #NNN` (one line per issue) at the top of its body and in the commit message so merging
closes it, with `Towards #NNN` only for deliberately partial work.

The live-preview engine listener (`--live <port>` in `Paradise.Sample.Runtime`) belongs to
`ParadiseGodotEditor` and is a separate change there. `docs/live-preview.md` specifies what it
needs to do; `tools/mock_runtime.py` is the executable specification.

## Style

- Match the surrounding code: type hints, `from __future__ import annotations`.
- Code explains itself; comments explain why. Prefer a name, a type, a small function, or an
  assertion over a comment that says what the code does, and restructure before commenting.
  A comment or docstring is for what code cannot say: a Blender-API gotcha, a constraint, a
  decision and its rejected alternative, a failure mode someone would "fix" back in, a
  cross-language contract. Delete comments that narrate control flow or restate the next line;
  private helpers whose name says what they do get no docstring.
- Warnings to the author should say what will go wrong at runtime and how to fix it, not just
  what was skipped. The Godot host's messages are the tone to match.
