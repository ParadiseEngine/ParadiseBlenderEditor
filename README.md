# ParadiseBlenderEditor

Use Blender as an authoring editor for [Paradise Engine](https://github.com/ParadiseEngine/ParadiseEngine):
mark objects as entities, export the engine-neutral scene contract, **play** the scene in the
standalone .NET runtime, and **live-preview** it while you keep editing.

This is a second authoring front end to the same contract `ParadiseGodotEditor` writes. Both
produce identical `data/` output, so a project can be authored in either tool — or in both.

```
Blender  ──authoring──▶  data/scenes/<Scene>.json      ──▶  paradise-runtime
                         data/materials/*.json
                         data/Models/*.glb
                         data/scenes/<Scene>.navmesh.bin
```

## Requirements

| | |
|---|---|
| **Blender 4.2+** | developed against 5.1; the extension system landed in 4.2 |
| **`paradise-runtime`** | `dotnet tool install --global Paradise.Sample.Runtime` — needed for Play and live preview |
| .NET SDK 10+ | *optional* — only for navmesh baking and the contract conformance check |
| KTX-Software (`toktx`) | *optional* — but the engine's glTF reader rejects PNG/JPEG, so textured meshes need it |

## Install

For development, symlink this working copy into Blender's extensions directory:

```bash
python3 tools/install_addon.py
```

Then restart Blender and enable **Paradise Engine Tools** in Preferences > Add-ons.

## Use

Everything lives in the **Paradise** tab of the 3D viewport sidebar (`N`).

1. **Set the data directory** (Scene panel). Defaults to `//data`, next to the .blend.
2. **Mark entities.** Select objects → **Make Paradise Entity**. Only marked objects export.
   Entity-ness is a flag, not a node type, so it is invisible in the outliner — the panel's
   entity count and **Select Paradise Entities** are how you find them again.
3. **Save**, or press **Export Paradise Scene**. Saving re-exports by default.
4. **Play** launches the runtime on the exported data.
5. **Start Live Preview** launches the runtime and streams your edits to it as you work.

## Testing

```bash
./tools/run_tests.sh
```

Three layers, each catching something the others cannot:

| layer | what it proves |
|---|---|
| `tests/unit` | the contract math is self-consistent (fast, no Blender) |
| `tests/integration` | our axis conversion agrees with **Blender's own glTF exporter**, and the live protocol round-trips |
| `contract-check` | our JSON round-trips through the **engine's own C# reader/writer** |

The unit tests would happily pass with a completely wrong axis convention — only the
integration layer catches that. And only the conformance gate catches contract drift.

## Documentation

- [`docs/quickstart.md`](docs/quickstart.md) — first entity to first render
- [`CONVENTIONS.md`](CONVENTIONS.md) — how Blender's conventions map to the contract's, and where they differ
- [`docs/live-preview.md`](docs/live-preview.md) — the live protocol, and the engine-side work it still needs
- [`docs/parity.md`](docs/parity.md) — feature-by-feature comparison with the Godot addon

## Status

Export and Play are complete and verified end to end. **Live preview is complete on the Blender
side only**: `paradise-runtime` has no `--live` listener yet — that is a separate change in the
`ParadiseGodotEditor` repository. Until it ships, the preview drives `tools/mock_runtime.py`,
which implements the protocol and is what the integration test uses. See
[`docs/live-preview.md`](docs/live-preview.md).
