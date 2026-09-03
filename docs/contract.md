# The export contract, from Blender's side

What this addon writes, where it goes, and how it is verified. The contract itself is defined
by C# `Paradise.Export`; this describes the Blender host's view of it. For where Blender's
conventions differ, see [`../CONVENTIONS.md`](../CONVENTIONS.md).

## Output layout

Everything is written under the scene's data directory (default `//data`). The runtime resolves
**every** contract path relative to it — an asset referenced from anywhere else is unreachable,
which is why the exporter warns rather than exporting a path that will never load.

```
data/
  scenes/<Scene>.json           the level document
  scenes/<Scene>.navmesh.bin    DotRecast MeshSet binary
  materials/<name>.json         one document per material
  Models/<mesh>.glb             entity geometry, one per mesh datablock
  sprites/<sheet>.ktx2          spritesheets (KTX2 sidecars)
  ProjectSettings.json          physics and renderer tuning
```

`<Scene>` is the .blend's basename by default, overridable per scene.

## Document shape

`LevelData` (`contract/schema.py` mirrors it field for field, in declaration order — System.Text.Json
writes properties in that order, so preserving it keeps a Blender export diffable against a
Godot export of the same scene):

```jsonc
{
  "SchemaVersion": 6,
  "Entities": [
    [ { "Id": "<component guid>", "Type": "<CLR name>", "Data": { /* … */ } }, /* … */ ],
    /* one array per object */
  ]
}
```

**An object IS its authored components, and the document is nothing but a list of them.** Schema v5
deleted the entity record and every document-level block. What an entity used to state as fields is
either a component now or gone:

| was | is |
|---|---|
| `Id` | the format's `meta` payload (`Guid`, `Name`, `Parent`) — since v6 an identity again, and the hierarchy with it |
| `WorldMatrix` | the format's `transform` payload — LOCAL position, rotation, scale; composition is the loader's |
| `IsActive: false` | the object is not written at all |
| `Camera` (document) | gone — it was the editor's viewport, and it framed any scene authoring no rig |
| `Lighting.States[n].Environment` | `EnvironmentData`, on an object of its own (the shadow settings moved with it) |
| `Lighting.States[n].Lights[i]` | `SceneLightData` on the lamp's OWN object — every lamp is an object |
| `StableId`, `DisplayName`, `Kind`, `SpawnPhase`, `Prefab*`, `InitialAnimation`, `Parent`, `Local*`, `Overrides`, `NavMeshAgent`, `Interactables`, `NavMeshFile`, `Materials` | gone — written by a host, read by nobody |

Encoding rules, all enforced by `contract/writer.py`:

- **Every field is written, including nulls.** The C# writer uses
  `DefaultIgnoreCondition = Never`; omitting a key changes the document's shape.
- **Vectors and quaternions are flat float arrays.** Quaternions are `[x, y, z, w]` —
  System.Numerics order, *not* Blender's `(w, x, y, z)`.
- **Matrices are 16 floats, column-major, column-vector layout** — translation lands at flat
  indices **12/13/14**. (A uniformly scaled entity at the origin reads
  `[s,0,0,0, 0,s,0,0, 0,0,s,0, 0,0,0,1]`.)
- **`Color32` is written as `"#RRGGBBAA"`**, channels quantized to bytes, so values are `n/255`.
  Precision loss is the contract, not an approximation on our side. (The C# converter also
  accepts the older `{"r","g","b","a"}` object form on read; the writer emits the string.)
- **Enums serialize by name** (`"Box"`, `"Dynamic"`, `"Sprite"`).
- **Floats are float32**, printed with shortest-round-trip precision for 32 bits.
- **Writes are atomic** (temp file + rename), so a reader never sees a half-written document.

## Entity model

An entity is any object with `object.paradise.is_entity` set — and it is exported **only if it
authors something beyond its name and its placement.** An empty an author marked and never gave a
mesh, a collider or a component to is not a statement about the world.

Each exported object carries:

- **`Name`** — the Blender object's name, so a runtime refusal can say which object it is about
- **`Transform`** — its world matrix, rebased into contract axes
- plus whatever it authors:

| component | source |
|---|---|
| `Materials` | the object's Blender material slots — derived, never a form: a material assignment has nothing a picker could add |
| `Collider` | objects marked as colliders and assigned to this entity (the host's `authoredBy: shape` bake, drawn as a component in the Components panel) |
| a mesh | **authored**, not derived — a component with an `authoredBy: mesh` field, pointing at the object whose geometry to export. `Renderable` is no longer derived from an object merely having mesh data: what an object draws is something an author says |
| `Rigidbody` | authored `paradise.rigidbody` component; else derived alongside colliders (static, or kinematic when the entity authors an agent) |
| `Interactable` | presence of interaction colliders |
| `Agent` | authored `paradise.agent` component |
| `SpriteAnimation` | not authorable in this host yet — the schema marks it host-baked (`authoredBy: sprite`), and Blender has no sprite host object |
| `ParticleEmitter` | authored `paradise.particle-emitter` component (its `Sheet` is a host-baked asset reference, not baked here yet) |
| `AudioEmitter` | authored `paradise.audio-emitter` component |
| the game's own | schema-driven authored components (below) |

Two more objects come from the scene rather than from an entity: every **lamp** becomes an object
carrying `SceneLightData`, and one unnamed object carries the scene's `EnvironmentData`.

### Authored components

The game's own components, declared once as C# records marked `[Authored]`. Building the game
dumps their description to `<data>/authoring-schema.json`; the addon reads it
(`contract/authoring.py`), draws it in the entity panel's *Components* section
(`authoring/authored_components.py`), and exports each enabled component as
`{ "Id": "<schema id>", "Data": { … } }`. The engine never learns the type — the payload rides
along verbatim and the game deserializes it through its generated readers.

Entity identity used to be an authored `Identity` component spread onto the entity's own fields at
export — `Kind`, `IsActive`, `InitialAnimation`, `DisplayName`, `SpawnPhase`. There are no fields to
spread onto since v5 and no consumer for four of the five, so the component is gone; `IsActive` is
now expressed by simply not exporting the object. The engine's own components
arrive in the SAME document: a launcher built with `ParadiseAuthoringScanReferences` merges every
assembly it references into its dump, so `<data>/authoring-schema.json` describes the engine's
components and the game's alike. Nothing is vendored here any more — a checked-in copy could
disagree with the engine the game builds against, and being merged first it would have won. The
bridge's `engine-schema` verb still prints the engine's half on its own, as a diagnostic for when
a component is missing and you need to know which side dropped it.

### Host references (`authoredBy`)

Five kinds, four of them an object slot differing only in what the exporter bakes out of what you
point at:

| kind | picker | baked |
|---|---|---|
| `transform` | object slot | where it STANDS — `Position`/`Rotation`/`Scale`/`Yaw`, whichever the record declares |
| `shape` | object slot | the collider drawn ON it, as a whole `ColliderShapeData` |
| `mesh` | object slot | its geometry, written out as a GLB, baked as the data-relative field. **Pointing at itself is the normal case** — an object usually draws its own mesh |
| `entity` | object slot | its NAME, verbatim. The odd one: the reference itself travels, reduced to the one thing every exported object carries |
| `asset` | file field | a path under `data/`, filtered by the field's declared extensions |

The first two fill the LEAVES a record declares under the reference; the last three ARE the value
and are written at the reference's own path. An unassigned or dangling reference exports the
field's own empty value rather than vanishing — a field that is absent reads as "this host does not
implement the kind", while one present and empty reads as "nobody picked an object", and only the
second is something a loader can refuse by name.

The wire format is pinned by the Godot host (`AuthoredEntityCore.ValueOf`) and by
`tests/unit/test_authoring.py`: every schema field written at its schema type, defaults filling
unset ones; enums by member name; vectors as float arrays; colours as `{r,g,b,a}`; an empty
string with no declared default as `null`.

The schema hot-reloads whenever the file's (mtime, size) stamp moves, so a game rebuild shows
up in the panel without restarting Blender.

Audio event names are exported **verbatim and unvalidated**. The events exist only inside the
audio project (a Wwise `.wproj`), which the addon has no visibility into, and the middleware
resolves a name to an id by hashing it when banks are generated — so the exporter cannot tell a
typo from an event nobody has authored yet, and refusing to export an unknown name would make the
addon depend on a tool it cannot see. An unrecognized name is a silent emitter, which is what the
audio tool does with it too.

## Verification

Three layers, each catching what the others cannot.

**Unit tests** (`tests/unit`, no Blender) pin the contract math against itself: the axis
algebra, the float32 formatting (values taken from the engine's own golden fixture), the
half-away-from-zero rounding in `Color32`, the collider fold, the sky integration's factor of π.

**Integration tests** (inside Blender) pin the conversion against *independent* implementations:
`test_axis_parity.py` compares our basis change to Blender's own glTF exporter across six
transforms, and `test_export_scene.py` asserts contract values on a real exported scene.

**The conformance gate** is the only thing that can catch contract drift:

```bash
dotnet run --project tools/ParadiseBlenderBridge -- contract-check data/scenes/MyScene.json --verbose
```

It reads the Python-written document with the engine's own `ExportJsonReader`, re-serializes it
with `ExportJsonWriter`, and compares semantically. A key the reader ignored (a typo, or a field
Python invented) vanishes on the way out and is reported; so is a missing field, a wrong nesting
level, an enum written as a number, or a value that does not survive the round trip.

Comparison is **value-based** — `5` and `5.0` are equal, matching the engine's stated contract.
Byte-identical output is a bonus that the current float formatting achieves; `--verbose` reports
when the documents are semantically equal but textually different.

## Changing the contract

The engine's `LevelDocument.cs` is the source of truth. Mirror the change in
`contract/schema.py` (same field order, same defaults), bump `SCHEMA_VERSION` only in lockstep
with `LevelData.CurrentSchemaVersion`, and run `contract-check` against a real export before
trusting it.
