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
The same install also links **Paradise Assets** (open `assets/` documents; the `.blend` is a cache).

## Agent setup

Paste the block below into a coding-agent chat in a workspace that contains this repository.
The agent should run the steps, not recap them.

```
Set up the Paradise Blender addons on this machine.

This repo (ParadiseBlenderEditor) ships two extensions. Link and enable both; they can run together:
- Paradise Engine Tools (paradise_blender) — the .blend is the source of truth; exports to data/
- Paradise Assets (paradise_assets) — assets/ is the source of truth; the .blend under .editor/blend/ is a disposable cache

Do the work with the shell and a headless Blender. Do not ask me to click through Preferences unless a path is genuinely ambiguous.

1. Locate this repo (ParadiseBlenderEditor, the directory that contains tools/install_addon.py).

2. Locate Blender 5.2+ (paradise_assets refuses to enable below 5.2). Typical binary:
   macOS: /Applications/Blender.app/Contents/MacOS/Blender
   If several versions exist, use the newest that is >= 5.2. Do not pass --factory-startup: that would throw away the prefs you write.

3. From the repo: python3 tools/install_addon.py
   That symlinks both packages into Blender's extensions/user_default. Blender must have been launched once before so that config directory exists.

4. Enable both addons in user preferences (default_set + persistent, then save):

   blender --background --python-expr '
   import addon_utils, bpy
   for mod in (
       "bl_ext.user_default.paradise_blender",
       "bl_ext.user_default.paradise_assets",
   ):
       addon_utils.enable(mod, default_set=True, persistent=True)
       loaded, _ = addon_utils.check(mod)
       print(mod, "loaded=" + str(loaded))
   bpy.ops.wm.save_userpref()
   '

   If the module names are not under user_default, list bpy.context.preferences.addons.keys() and enable the paradise_blender / paradise_assets entries you find.

5. Point Paradise Assets at the toolchain. Discover these; do not invent them:
   - cli: Paradise.Cli.csproj in a ParadiseEngine checkout (src/Paradise.Cli/Paradise.Cli.csproj), or a `paradise` executable on PATH / the installed dotnet tool. Prefer the .csproj in a sibling engine checkout when one exists.
   - runtime_host: the GAME's launcher, not the engine sample. Look for a *.Launcher.csproj (or similar host .csproj) next to an assets/project.toml. For ShiningPie that is ShiningPie.Launcher/ShiningPie.Launcher.csproj.
   - ktx_path: the ktx EXECUTABLE (KTX-Software v5 `ktx`, not a directory, not toktx for this addon). Probe `which ktx`, $PARADISE_KTX_PATH, /usr/local/bin/ktx, /opt/homebrew/bin/ktx. Blender launched from the Dock/Finder does not inherit the shell PATH, so store an absolute file path even if `which ktx` works in the terminal. A directory looks configured and is ignored.

   Write them with blender --background --python-expr, then bpy.ops.wm.save_userpref(). Print the three values you set.

6. Optionally set Paradise Engine Tools prefs the same way (runtime_host, ktx_path, and tools/ParadiseBlenderBridge/ParadiseBlenderBridge.csproj as bridge_project) if this machine will export .blend → data/. Skip if we only author assets/ documents.

7. Leave auto_watch on (default). Do not start `paradise assets watch` yourself as part of setup.

8. Tell me to fully quit and reopen Blender (a running session will not pick up a first-time enable). Summarize: blender binary used, extensions dir linked, and the absolute paths written to prefs. Do not commit anything.
```

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
