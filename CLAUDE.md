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
  pipeline/       KTX2 transcoding, .NET bridge invocation
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
