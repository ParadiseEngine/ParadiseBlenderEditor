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

## 7b. Tune the game

Authored components live in two places, both drawn from the same `data/authoring-schema.json`
your game's build dumps:

| Where | What it is | Where you edit it |
|---|---|---|
| On an entity | the object's own `Components.Custom` | **Components** panel |
| In a file | a JSON document of authored payloads | **Config** panel |

If your game keeps tunables in a JSON document — a `Components` array of `{"Id", "Data"}`
payloads, the same shape components travel in on an entity — press **+** in the **Config** panel
and pick it. The picker lists the config documents it finds under your data directory, so there is
no path to type and no way to name a file the runtime could not reach; scene exports, material
documents and the authoring schema are not offered. A group holding a LIST draws each row in its
own box with **+**, **X** and up/down buttons — including a list nested inside a row, such as the
weighted entries of a drop table. The panel then draws every group with the
units, ranges and tooltips the game declared in C#.

The list takes as many documents as you like: a game's tunables and a level's settings are two
rows, not two features. Each row keeps its own edited values, so two documents declaring the same
component id never collide.

Load and Save are buttons rather than automatic, deliberately: those files are the game's source
of truth and are edited by hand as well, so nothing writes to them behind you. A save merges into
the document rather than rebuilding it, so comments and any sections the addon does not understand
are left exactly as they were.

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

## 9. Creating prefabs (the Paradise Assets tab)

Everything above belongs to `paradise_blender`, which exports a `.blend` to `data/`. The
**Paradise Assets** tab is the other addon: it opens an `assets/**/*.prefab` document and writes
that document back. Two of its buttons create prefabs rather than edit one.

Both need the asset watcher running, and will start it for you. A new file has no identity until
`paradise assets watch` writes its `.meta`, and until it has one nothing can reference it — so a
creation without a watcher is refused rather than half-done. If the `paradise` CLI is not on
PATH or set in the addon preferences, neither button can work.

**Extract…** (Prefab Document panel, beside *Add Prefab…*) turns the active object and
everything parented under it into a new `.prefab`, and leaves an instance of it where the
subtree was. The instance keeps the object's identity, its name and where it stands, so
anything that referenced that object still does; the prefab holds the shape, with its root at
the origin. Two consequences worth knowing before pressing it:

- A reference from elsewhere in the level to a **child** of what you extracted breaks. A
  resolved child's identity is minted per instance, so the authored one stops naming anything.
  The operator warns, once per reference, and extracts anyway.
- The document root cannot be extracted (that would be "make an instance of the whole level"),
  and the operator refuses a path that already holds a prefab or a stray `.meta`.

Save your placement changes first: the extraction works on the file, so it refuses to run while
the scene holds edits the document does not have.

**Model Prefabs** (a panel of its own) keeps one prefab per model. *Generate Model Prefabs* runs
one pass: every `.glb` under `assets/` that no generated prefab stands for gets one at
`prefabs/models/<name>.prefab`, and a model that moved or was renamed has its prefab's mesh
reference refreshed. The prefab itself is never renamed — its guid is what levels reference, and
moving the file would break them to fix nothing.

Pick two components in that panel, static and skinned. The mirror reads each `.glb` to see
whether it carries a rig, and a rigged model authored as a static one is a prefab that loads,
shows the mesh, and is the wrong kind of thing in the game — so it is never guessed. A project
with no rigged models needs only the static one. With no schema at all, build the game's
launcher once.

A prefab is written only when the model has nowhere to be placed from yet. If anything already
references its mesh — the prefab written last time, one moved elsewhere since, or a document that
adopted the mesh by hand — `extract` leaves it alone and says so. Nothing tracks the pair
afterwards: a prefab you edit is yours, and deleting a model leaves its prefab behind for
`assets verify` to report as a dangling reference.
