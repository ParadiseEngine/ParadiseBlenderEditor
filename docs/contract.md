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
| `Renderable` | the object's mesh, exported to a GLB, or an authored model path |
| `Collider` | objects marked as colliders and assigned to this entity |
| `Rigidbody` | emitted alongside colliders; type from the dynamic/agent flags |
| `Interactable` | presence of interaction colliders |
| `Agent` | the agent flag and its movement fields |
| `SpriteAnimation` | the sprite section |
| `ParticleEmitter` | the particle section, when its kind is not None |

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
