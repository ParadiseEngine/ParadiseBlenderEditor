# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A Blender addon that authors scenes for Paradise Engine, exports them to the engine-neutral
data contract, launches them in the standalone runtime, and live-previews them while editing.

It is the **second** implementation of a contract whose reference implementation is C#
`Paradise.Export` (used by `ParadiseGodotEditor`). That framing matters for almost every
decision here: when this repo and the contract disagree, the contract is right.

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
load, and the failure is invisible: export-on-save and GUID maintenance simply stop working
after the user opens another .blend.

**Never block the main thread in `live/`.** Blender's UI is single-threaded and the peer is a
game runtime that stalls for frames at a time. Sends go through a bounded queue drained by a
background thread.

**An export reuses unchanged artifacts, and the rule for what may be cached is strict.**
`pipeline/cache.py` stores KTX2 transcodes and navmesh bakes under `<project>/.paradise-cache/`,
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
deliberately five members (`is_entity`, `entity_guid`, `model_path`, the two collider lists);
every component — the engine's identity/agent/rigidbody/audio/particles included — is authored
through the schema in the Components panel and routed to its typed slot by
`contract/authoring_router.py`. Do not add fixed fields back: the ~40-field mirror this
replaced is exactly the thing a schema change silently drifts away from. The engine's schema
is a vendored JSON (`contract/engine_authoring_schema.json`) because Python cannot read the C#
constant — the conformance suite compares it against the bridge's `engine-schema` verb, and a
drift there means "regenerate the file", not "patch it by hand".

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
commit spanning repos.**

The live-preview engine listener (`--live <port>` in `Paradise.Sample.Runtime`) belongs to
`ParadiseGodotEditor` and is a separate change there. `docs/live-preview.md` specifies what it
needs to do; `tools/mock_runtime.py` is the executable specification.

## Style

- Match the surrounding code: type hints, `from __future__ import annotations`, module
  docstrings that explain *why* rather than restating the code.
- Comments earn their place by recording a non-obvious constraint or a decision someone would
  otherwise "fix" into a bug. There are several of those here; keep them.
- Warnings to the author should say what will go wrong at runtime and how to fix it, not just
  what was skipped. The Godot host's messages are the tone to match.
