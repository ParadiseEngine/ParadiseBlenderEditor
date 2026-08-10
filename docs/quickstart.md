# Quickstart — first entity to first render

## 1. Prerequisites

```bash
dotnet tool install --global Paradise.Sample.Runtime   # provides `paradise-runtime`
```

Blender 4.2 or newer. Optional but recommended:

- **KTX-Software** (`toktx`) — the engine's glTF reader rejects PNG/JPEG, so textured meshes
  need KTX2 sidecars. Without it, exports still succeed; meshes just render untextured.
- **.NET SDK 10+** — only for navmesh baking and the contract conformance check.

## 2. Install the addon

```bash
python3 tools/install_addon.py
```

Restart Blender, then enable **Paradise Engine Tools** in Preferences > Add-ons. Point the
optional tool paths (`toktx`, the bridge project) at your installs in the addon preferences.

## 3. Set up the project

Save your .blend first — the default data directory is `//data`, relative to the .blend file,
and an unsaved file has nothing to be relative to (the addon falls back to a temp directory and
says so in the Scene panel).

Open the **Paradise** tab in the 3D viewport sidebar (`N`) and check the Scene panel:

```
Data Directory:  //data
Output: scenes/<your-blend-name>.json
Export On Save:  ✓
```

## 4. Author an entity

1. Add a mesh — a cube will do.
2. With it selected, press **Make Paradise Entity**.

That is the whole requirement. Only marked objects export; everything else in the scene is
ignored, so you can keep reference geometry, rigs, and layout helpers around freely.

Give it a material if you like: an ordinary Principled BSDF is read directly (base colour,
metallic, roughness, emission, normal map).

## 5. Give it collision

The engine needs explicit collider shapes — mesh geometry is for rendering.

1. Add an Empty (`Add > Empty > Cube`) and size it to cover the object.
2. Press **Make Paradise Collider** in the Collider panel, and pick a shape.
3. Parent it to your entity.
4. Select the collider, then ctrl-click the entity to make it active, and press **+** next to
   *Physics Colliders* in the entity's Physics panel.

An entity with colliders becomes a static body by default; tick **Dynamic Body** for something
the simulation should push around.

## 6. Add a camera and a light

An ordinary Blender camera and a sun lamp. The scene's active camera is the one exported.

## 7. Export and play

Save the .blend — that exports automatically. Or press **Export Paradise Scene**.

You should see:

```
data/
  scenes/<name>.json
  scenes/<name>.navmesh.bin      (if you have walkable geometry and the .NET bridge)
  materials/*.json
  Models/*.glb
  ProjectSettings.json
```

Now press **Play in Paradise**. The runtime opens in an SDL window with the engine's PBR
renderer.

If the window does not appear, the launch log is at `$TMPDIR/paradise_play.log`.

## 8. Live preview

Press **Start Live Preview** instead of Play. The runtime launches and then follows your edits
as you make them.

**This needs an engine-side listener that does not exist yet** — see
[`live-preview.md`](live-preview.md). The Blender half is complete; you can exercise it against
`tools/mock_runtime.py` today.

## Troubleshooting

**"No Paradise entities were found"** — nothing is marked. Select objects and press
*Make Paradise Entity*. Entity-ness is a flag, not a node type, so it is invisible in the
outliner; use **Select Paradise Entities** to see what is marked.

**Meshes render untextured** — `toktx` is not installed or not configured. The engine's glTF
reader requires KTX2. Install KTX-Software, set its path in preferences, and press
**Convert Textures To KTX2**.

**"references … outside the data directory"** — the runtime resolves every contract path under
`data/`, so an asset anywhere else is unreachable. Move it under the data directory.

**Objects appear rotated 90°** — this should be impossible; the axis conversion is pinned
against Blender's own glTF exporter by `tests/integration/test_axis_parity.py`. If you see it,
run that test and file the output.

**Live preview lags, or reports dropped messages** — lower the update rate in preferences. The
send queue is bounded on purpose, so a runtime that cannot keep up loses old messages rather
than growing Blender's memory without limit.
