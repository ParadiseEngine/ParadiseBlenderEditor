# Quickstart — from a checkout to a document you can play

This walks a real game project (ShiningPie is the one it was written against) from "Blender does
not know about Paradise" to "the game is running on the level I just edited".

## 1. Prerequisites

- **Blender 5.2+.** Below that the extension refuses to enable rather than degrading.
- **The `paradise` CLI.** Either `Paradise.Cli.csproj` from a ParadiseEngine checkout, or the
  installed dotnet tool. This is not optional: the CLI's watcher is what mints identities for
  new files, and without an identity a new prefab cannot be created at all.
- **KTX-Software (`ktx`)** — optional, but the engine's glTF reader rejects PNG/JPEG, so a build
  without it gives you untextured meshes.

## 2. Install the addon

```bash
python3 tools/install_addon.py
```

Restart Blender — a running session will not pick up a first-time enable — then enable
**Paradise Assets** in Preferences > Add-ons.

In its preferences, set:

| | |
|---|---|
| **Paradise CLI** | the `paradise` executable, or `src/Paradise.Cli/Paradise.Cli.csproj` |
| **Build Profile** | which `[build.profiles.*]` in `assets/project.toml` a Play build uses (`dev`) |
| **KTX Executable** | the `ktx` binary **itself**, not the directory holding it |
| **Watch While a Document Is Open** | leave on |

Absolute paths, even for things `which` finds: Blender launched from the Dock or Finder does not
inherit your shell `PATH`, and a preference that points at a directory looks configured and
behaves exactly like a blank one.

## 3. Open a document

Open the **Paradise** tab in the 3D viewport sidebar (`N`). With nothing open you get:

```
▾ Prefab Document
    No document open.
    [ Open Prefab… ]
    Recently opened here:
      shiningpie
      triggers

▾ Project
    ShiningPie   /…/paradise-workspace/ShiningPie
    ○ not watching                      [ Start ]
    [ Build ] [ Verify ]
    [ Clean ] [ Catalogue ]
```

The Project panel appears whenever this `.blend` sits inside a project — the working files under
`.editor/blend/` do, so it is there before you open anything.

**Open Prefab…** and pick `assets/levels/shiningpie.prefab`. Three things happen:

1. If a working `.blend` exists under `.editor/blend/levels/shiningpie.blend`, it is opened first
   — that is where your camera and selection live. Its objects are then rematerialized from the
   document regardless, so an edit made by another tool is never shown stale.
2. The document's objects appear, meshes instanced from the GLBs the document references.
3. `paradise assets watch` starts for the project.

## 4. Place something

**Add Prefab…** picks a prefab from the project and instances it. To drag with thumbnails
instead, hit **Catalogue** once (it renders every prefab in a background Blender — minutes on a
cold cache, seconds afterwards), then open an Asset Browser and choose the project's library from
the dropdown.

Move, rotate and scale with Blender's own gizmos. The document is Y-up and Blender is Z-up; you
never see that, because `document/axes.py` rebases on the way in and out.

An instance's *children* are the prefab's, not yours: moving one is refused on save, because the
document has no way to say it. Move the instance, or edit the prefab it came from.

## 5. Edit components

Select an object. The **Components** panel shows what the document says about it:

- `meta` and `transform` are drawn **locked**. Blender's name field and transform gizmo are their
  editor, and a second way to type an identity is a second thing that can disagree.
- The game's own components are editable where the game's schema says they are. A field marked
  `[AuthoredByHost]` is shown locked too — its value comes from the object it points at.
- **Add Component** offers what the schema describes.

If the panel says *"No game schema — build the launcher to edit game fields"*, that is a fresh
clone or a `clean` that took `.editor/` with it. The button beside it is the build that fixes it
(`paradise host build`, which dumps `.editor/authoring-schema.json`).

Edits are held as an overlay until you save, so a component nobody edited is written back
byte-for-byte — including ones this addon has no schema for.

## 6. Save

**Ctrl+S** writes the document first and the working `.blend` second. The **Save** button in the
Prefab Document panel writes only the document.

A save is refused if the document changed on disk since it was opened; the panel says so, and
your work stays in the working file. Reload, then redo the edit.

## 7. Play

**Build & Play** runs `paradise host play`: the CLI compiles `assets/` into `build/`, brings the
game's launcher up to date, and runs it on the open document. A failed build therefore stops the
launch rather than running the last good one.

**Watch & Play** does the same under `dotnet watch`, so a C# edit is hot-patched into the running
game. Slower to start; no rebuild afterwards.

If the panel says *"No `[host]` project in assets/project.toml"*, the project has not declared
which launcher is its game — that is the project's business, not a preference, so that a script
and CI run the same game the same way.

## 8. New models

Drop a `.glb` under `assets/` and run:

```bash
paradise assets extract
```

It writes a prefab for any model that has none, so the model is placeable straight away. Where
that prefab lands is `[glb] extract` in the model's own sidecar, else `[extract] directory` in
`project.toml`, else beside the model — so "is there a prefab beside the model" is the wrong
question on a project that configures either of the first two.

A generated prefab is a **seed, not a projection**: from the moment it is written it is an
ordinary document you own. Nothing records which model it came from, nothing updates it, and
nothing deletes it.

## Where things live

| | |
|---|---|
| `assets/` | the source of truth — documents, models, textures, and a `.meta` beside each |
| `.editor/blend/` | working `.blend` files, one per document. Disposable |
| `.editor/asset-library/` | the Asset Browser catalogue and its thumbnails. Disposable |
| `.editor/authoring-schema.json` | the game's component schema, dumped by its launcher build |
| `build/` | what the CLI compiles and the game loads. Disposable |

Everything under `.editor/` and `build/` is regenerable, and `Clean` deletes `build/` (and
`.editor/` too, if you tick the box).
