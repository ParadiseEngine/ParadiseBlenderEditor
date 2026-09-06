# ParadiseBlenderEditor

Author [Paradise Engine](https://github.com/ParadiseEngine/ParadiseEngine) content in Blender:
open a prefab document out of a game's `assets/` tree, place things in it with Blender's own
tools, edit its components, and **play** the game on what you just authored.

`assets/` is the source of truth. The `.blend` is a disposable cache of one document, kept under
`.editor/blend/` so a session's camera and selection survive a reopen; the `paradise` CLI
compiles `assets/` into `build/`, and the game loads that.

```
assets/levels/*.prefab  ──Blender──▶  assets/levels/*.prefab
        │                             (the document is read and written; the .blend is a cache)
        └──paradise assets build──▶  build/  ──▶  the game
```

## Requirements

| | |
|---|---|
| **Blender 5.2+** | the manifest's floor; Blender refuses to enable the extension below it |
| **the `paradise` CLI** | `Paradise.Cli` from a ParadiseEngine checkout, or the installed dotnet tool — needed for the asset watcher, Build and Play |
| KTX-Software (`ktx`) | *optional* — but the engine's glTF reader rejects PNG/JPEG, so textured meshes need it. The CLI does the transcode; the addon only passes the path along |

Nothing here is .NET. The addon is pure Python and shells out to the CLI.

## Install

For development, symlink this working copy into Blender's extensions directory:

```bash
python3 tools/install_addon.py
```

Then restart Blender and enable **Paradise Assets** in Preferences > Add-ons. Point
**Paradise CLI** (and `ktx`, if you have it) at your installs in the addon preferences — Blender
launched from the Dock or Finder does not inherit your shell `PATH`, so store absolute paths.

## Agent setup

Paste the block below into a coding-agent chat in a workspace that contains this repository.
The agent should run the steps, not recap them.

```
Set up the Paradise Blender addon on this machine.

This repo ships one extension: Paradise Assets (paradise_assets) — assets/ is the source of
truth; the .blend under .editor/blend/ is a disposable cache.

Do the work with the shell and a headless Blender. Do not ask me to click through Preferences
unless a path is genuinely ambiguous.

1. Locate this repo (ParadiseBlenderEditor, the directory that contains tools/install_addon.py).

2. Locate Blender 5.2+ (the addon refuses to enable below it). Typical binary:
   macOS: /Applications/Blender.app/Contents/MacOS/Blender
   If several versions exist, use the newest that is >= 5.2. Do not pass --factory-startup: that
   would throw away the prefs you write.

3. From the repo: python3 tools/install_addon.py
   That symlinks the package into Blender's extensions/user_default. Blender must have been
   launched once before so that config directory exists.

4. Enable the addon in user preferences (default_set + persistent, then save):

   blender --background --python-expr '
   import addon_utils, bpy
   mod = "bl_ext.user_default.paradise_assets"
   addon_utils.enable(mod, default_set=True, persistent=True)
   loaded, _ = addon_utils.check(mod)
   print(mod, "loaded=" + str(loaded))
   bpy.ops.wm.save_userpref()
   '

   If the module name is not under user_default, list bpy.context.preferences.addons.keys() and
   enable the paradise_assets entry you find.

5. Point it at the toolchain. Discover these; do not invent them:
   - cli: Paradise.Cli.csproj in a ParadiseEngine checkout (src/Paradise.Cli/Paradise.Cli.csproj),
     or a `paradise` executable on PATH / the installed dotnet tool. Prefer the .csproj in a
     sibling engine checkout when one exists.
   - (The game's launcher is NOT a preference: it is `[host] project` in the game's
     assets/project.toml, relative to the project root -- for ShiningPie,
     `ShiningPie.Launcher/ShiningPie.Launcher.csproj`. Play and Build Game Schema run
     `paradise host play` / `paradise host build` on it.)
   - ktx_path: the ktx EXECUTABLE (KTX-Software v5 `ktx`, not a directory, not toktx). Probe
     `which ktx`, $PARADISE_KTX_PATH, /usr/local/bin/ktx, /opt/homebrew/bin/ktx. Blender launched
     from the Dock/Finder does not inherit the shell PATH, so store an absolute file path even if
     `which ktx` works in the terminal. A directory looks configured and is ignored.

   Write them with blender --background --python-expr, then bpy.ops.wm.save_userpref(). Print the
   values you set.

6. Leave auto_watch on (default). Do not start `paradise assets watch` yourself as part of setup.

7. Tell me to fully quit and reopen Blender (a running session will not pick up a first-time
   enable). Summarize: blender binary used, extensions dir linked, and the absolute paths written
   to prefs. Do not commit anything.
```

## Use

Everything lives in the **Paradise** tab of the 3D viewport sidebar (`N`), as four panels:

| panel | scope | what it is for |
|---|---|---|
| **Prefab Document** | the open document | open, save, reload, recreate; add a prefab instance; extract a selection into a new prefab |
| **Project** | the project it lives in | the asset watcher, Build / Verify / Clean, the Asset Browser catalogue |
| **Play** | the open document | run the game on it, and say why it stopped |
| **Components** | the selected object | the document's components, editable where the game's schema says they are |

**Project** is available before any document is open — a `.blend` saved inside a project is
enough — so a fresh session can start the watcher and build without opening anything. When no
document is open, **Prefab Document** lists what you last worked on here, or what the project
holds if you have not worked on anything yet.

Right-clicking a **document object** — in the Outliner or in the viewport — adds two entries:

- **Open Prefab in New Blender** — for an instance (or anything under one), opens the prefab it
  came from in a *second* Blender. The level stays open in this one; the watcher reconciles what
  either writes.
- **Create Prefab from Object…** — the same extraction the sidebar offers, on the object you
  clicked.

A typical loop:

1. **Open a prefab** (or drag a `.prefab` into the viewport). The addon starts
   `paradise assets watch` for the project, which is what mints identities for new files.
2. **Place things.** Add Prefab… instances a document; the Asset Browser catalogue gives you
   thumbnails to drag from.
3. **Ctrl+S.** The document is written first, then the working `.blend`. The explicit **Save**
   button writes only the document. **Recreate Working File** deletes the cached `.blend` and
   rebuilds the scene from the document — the way out of a workfile that has accumulated
   something you do not want, a stuck viewport included. **Reload** keeps what the working file
   holds; Recreate does not, which is why it asks first.
4. **Build & Play.** The CLI builds `assets/` into `build/`, brings the launcher up to date, and
   runs the game on the open document.

## Testing

```bash
./tools/run_tests.sh
```

Two layers, each catching something the other cannot:

| layer | what it proves |
|---|---|
| `tests/unit` | the document format is self-consistent, and byte-identical to the C# writer (fast, no Blender) |
| `tests/integration` | our axis conversion agrees with **Blender's own glTF exporter**, and every operator survives a real project |

The unit tests would happily pass with a completely wrong axis convention — only
`tests/integration/test_axis_parity.py` catches that, which is why it is the one test to run
after touching anything under `document/` or `materialize/`.

Integration tests that need a real project take one via `PARADISE_ASSETS_PROJECT` (default
`../shiningpie`) and skip cleanly when it names nothing.

## Documentation

- [`docs/quickstart.md`](docs/quickstart.md) — from a checkout to a document open and playing
- [`CONVENTIONS.md`](CONVENTIONS.md) — where Blender's conventions differ from the document's
- [`CLAUDE.md`](CLAUDE.md) — the things that will bite you, and why they are the way they are
