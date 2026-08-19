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
  "SchemaVersion": 2,
  "Camera": { "Position": [...], "Rotation": [...], "OrthographicSize": 5, "BackgroundColor": {...} },
  "Lighting": { "ActiveState": "Default", "States": [ { "Name": "Default", "Environment": {...}, "Lights": [...] } ] },
  "NavMeshAgent": null,
  "Interactables": [],
  "Entities": [ /* LevelEntityData */ ],
  "NavMeshFile": "<Scene>.navmesh.bin",
  "Materials": []
}
```

Encoding rules, all enforced by `contract/writer.py`:

- **Every field is written, including nulls.** The C# writer uses
  `DefaultIgnoreCondition = Never`; omitting a key changes the document's shape.
- **Vectors and quaternions are flat float arrays.** Quaternions are `[x, y, z, w]` —
  System.Numerics order, *not* Blender's `(w, x, y, z)`.
- **Matrices are 16 floats, column-major, column-vector layout** — translation lands at flat
  indices **12/13/14**. (A uniformly scaled entity at the origin reads
  `[s,0,0,0, 0,s,0,0, 0,0,s,0, 0,0,0,1]`.)
- **`Color32` is `{"r","g","b","a"}`** with channels quantized to bytes, so values are `n/255`.
  Precision loss is the contract, not an approximation on our side.
- **Enums serialize by name** (`"Box"`, `"Dynamic"`, `"Sprite"`).
- **Floats are float32**, printed with shortest-round-trip precision for 32 bits.
- **Writes are atomic** (temp file + rename), so a reader never sees a half-written document.

## Entity model

An entity is any object with `object.paradise.is_entity` set. Each exports:

- **identity** — a minted GUID, stable per placement (`CONVENTIONS.md` §4)
- **transforms** — local (relative to the parent entity) and world, both rebased into contract axes
- **material slots** — contract fields in slot order, matching the referenced GLB's primitive order
- **components**, each present or `null`:

| component | source |
|---|---|
| `Renderable` | the object's mesh, exported to a GLB, or an authored model path — derived, never a form |
| `Collider` | objects marked as colliders and assigned to this entity (the host's `authoredBy: shape` bake, drawn as a component in the Components panel) |
| `Rigidbody` | authored `paradise.rigidbody` component; else derived alongside colliders (static, or kinematic when the entity authors an agent) |
| `Interactable` | presence of interaction colliders |
| `Agent` | authored `paradise.agent` component |
| `SpriteAnimation` | not authorable in this host yet — the schema marks it host-baked (`authoredBy: sprite`), and Blender has no sprite host object |
| `ParticleEmitter` | authored `paradise.particle-emitter` component (its `Sheet` is a host-baked asset reference, not baked here yet) |
| `AudioEmitter` | authored `paradise.audio-emitter` component |
| `Custom` | schema-driven authored components (below); **absent** — not `null` — when nothing is authored |

### Authored components (`Components.Custom`)

The game's own components, declared once as C# records marked `[Authored]`. Building the game
dumps their description to `<data>/authoring-schema.json`; the addon reads it
(`contract/authoring.py`), draws it in the entity panel's *Components* section
(`authoring/authored_components.py`), and exports each enabled component as
`{ "Id": "<schema id>", "Data": { … } }`. The engine never learns the type — the payload rides
along verbatim and the game deserializes it through its generated readers.

Entity identity (`Kind`, `IsActive`, `InitialAnimation`, `DisplayName`, `SpawnPhase`) is the
authored `paradise.identity` component, spread onto the entity itself at export; an entity
without one exports the defaults (`Prop`, active, `LevelStart`). The engine's own schema is
vendored at `contract/engine_authoring_schema.json` (Python cannot read the C# constant) and
merged in front of the game's, exactly as the Godot host merges; the bridge's `engine-schema`
verb plus the conformance suite keep the copy honest.

The wire format is pinned by the Godot host (`AuthoredEntityCore.ValueOf`) and by
`tests/unit/test_authoring.py`: every schema field written at its schema type, defaults filling
unset ones; enums by member name; vectors as float arrays; colours as `{r,g,b,a}`; an empty
string with no declared default as `null`. Fields the schema marks `authoredBy` (host-object
bakes: shapes, node references, assets) are not authored in this host yet and export absent,
which the reader treats as unauthored.

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
