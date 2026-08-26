# Parity with the Godot addon

Feature-by-feature comparison with `ParadiseGodotEditor/addons/paradise/`, which is the
reference implementation of the export contract.

Legend: **✔** full parity · **≈** parity with a documented difference · **✖** not implemented

## Scene export

| contract area | Godot host | Blender host | notes |
|---|---|---|---|
| Entity transforms | ✔ verbatim | ≈ | Blender is Z-up, so transforms are rebased (`CONVENTIONS.md` §1). Pinned against Blender's glTF exporter. |
| Object identity | ✖ | ✖ | Schema v5 removed `EntityGuid` from the document; nothing exports one. `object.paradise.entity_guid` survives as host bookkeeping. What identifies an object on the wire is its `Name` component, which is for DIAGNOSTICS and is not unique. |
| Parent/child | ✖ | ✖ | The parent link went with the entity record. An object's placement is stated in world space, once. |
| Entity order | tree order | sorted by name | Blender guarantees no iteration order; sorting keeps exports diff-stable. |
| Mesh reference | ✔ shared GLB, derived | ≈ AUTHORED | The Godot host still derives a `Renderable` from the node's mesh. This host does not: what an object draws is a component an author attaches, pointing at the object whose geometry to export (`authoredBy: mesh`). Still exported per mesh datablock and deduplicated. |
| Materials | ✔ `BaseMaterial3D` | ≈ | Principled BSDF. Colour space is inverted (§2) — Blender is already linear. |
| Material slot order | ✔ | ✔ | Slot order, matching the GLB's primitive order. On `MaterialsComponentData` since v5 — its own component, because slots are not geometry. Still DERIVED from the object's material stack in both hosts. |
| Procedural recipes, transmission | ✔ resource metadata | ✔ | Property group on the material. |
| Colliders (box/sphere/capsule) | ✔ typed shapes | ≈ | Blender has no collision primitives; shape is marked and dimensions come from bounds or explicit values. The entity's reference lists are drawn as the Collider / Interactable components in the Components panel. |
| Collider scale folding | ✔ | ✔ | Same `ColliderScaleFold` rules, ported. |
| Collision layers | ✔ mask → index | ≈ | Authored as an index directly, making the lossy multi-layer case unrepresentable rather than merely warned about. |
| Rigidbody | ✔ | ✔ | Authored `paradise.rigidbody` wins; a derived body (static, or kinematic with an agent) is emitted alongside colliders. |
| Agent | ✔ | ✔ | The authored `paradise.agent` component, schema-driven in both hosts. |
| Sprite animation | ✔ from `Sprite3D` | ✖ | The schema marks the whole component host-baked (`authoredBy: sprite`); Blender has no sprite host object yet, so it is not authorable here. |
| Particle emitter | ✔ | ≈ | Authored `paradise.particle-emitter` component; the `Sheet` asset reference is host-baked and not baked here yet. |
| Prefabs | ✔ `PackedScene` | ✖ | Schema v5 removed every prefab field from the object (`Prefab`, `PrefabAssetPath`, `PrefabGuid`, `PrefabAssetType`, `NearestInstanceRoot`) and the prefab TEMPLATE document with them. This host no longer exports collection instances as prefabs. |
| Prefab overrides | ✖ (no Godot API) | ✖ | `PrefabOverrideData` is gone from the contract; neither host emits anything. |
| Camera | ✔ document block | ✖ | The document-level camera block is gone. It was the editor's viewport, and it framed any scene that authored no rig — which is not a decision anybody made. A game that wants a camera authors one as its own component. |
| Lights | ✔ | ≈ | Watts → multiplier is a calibration constant; area lights export as point lights. Spot angle is not doubled. EVERY lamp is now an object carrying `SceneLightData` — the scene-level light list is gone, so a light is described once and the rule against describing it twice has nothing left to break. |
| Environment / tone mapping | ✔ Godot `Environment` | ≈ | Tone mapping and background read from Blender; sky gradient, SSAO, glow, fog authored explicitly (§"What Blender cannot express"). |
| Ambient SH | ✔ | ✔ | Same integration and L2 projection code, ported. |
| Navmesh | ✔ `NavigationServer3D` | ✔ | Baked by `tools/ParadiseBlenderBridge` with DotRecast — the same library, same parameters, same binary. |
| `ProjectSettings.json` | ✔ | ✔ | Contract defaults; no Blender counterpart for any field. |
| Authored config documents | ✖ | ✔ | Authored components in FILES rather than on entities: a project lists any number of JSON documents (a game's tunables, a level's settings) and the Config panel draws each from the same authoring schema the per-entity panel uses. The Godot host has no equivalent — its **Paradise/Settings…** dialog is hand-rolled and edits `ProjectSettings.json`, a different file. Load and save are explicit: these files are the game's source of truth and are hand-edited, and a save merges into the document rather than rewriting it, so prose keys and content sections the addon does not understand survive (top-level keys only — a `Data` payload is replaced wholesale). Authored LISTS are editable here: add, remove and reorder rows, including a list nested inside a row. |
| KTX2 texture pipeline | ✔ | ✔ | Same `toktx` invocation. |
| UI asset staging | ✔ | ✖ | The Godot host stages `res://ui/**` into `data/ui/`. Noesis XAML is authored in Noesis Studio, not Blender, so there is nothing for this host to stage. |
| Authored components | ✔ `AuthoredEntityNode` | ✔ | Same `authoring-schema.json`, same wire format, hot-reloaded on game rebuild. An object IS its component list since v5, so there is no `Custom` bucket to be in. |
| Host references (`authoredBy`) | ✔ all kinds | ≈ five of six | This host bakes `transform`, `shape`, `mesh`, `entity` and `asset`. Only `sprite` is unimplemented (Blender has no sprite host object). The first two fill the LEAVES a record declares under the reference; the last three ARE the value. An unassigned reference exports the field's own empty value rather than vanishing — absent reads as "this host does not implement the kind", present-and-empty as "nobody picked one", and only the second is refusable by name. |
| Engine components via schema | ✔ | ≈ | Agent, rigidbody, audio and particles are schema-authored. Name, transform, materials and lights stay DERIVED from real Blender data — an exporter writes them for every object it emits, which is why they are engine components rather than any game's. `Identity` is gone: there are no entity fields left to spread onto. |

## Workflow

| | Godot host | Blender host |
|---|---|---|
| Export on save | ✔ | ✔ |
| Game rebuild on code change | ✔ editor Build button | ✔ background watcher (`pipeline/schema_build.py`), when the scene names its Game Project |
| Manual export | ✔ | ✔ |
| Play in the .NET runtime | ✔ | ✔ |
| **Live preview** | ✖ | ✔ *(Blender side; needs the engine listener — see `live-preview.md`)* |
| Entity visible in the outliner | ✔ (node type) | ✖ (a flag; panel + *Select Paradise Entities* compensate) |
| Runtime host auto-detection | ✔ | ✔ same order |
| `--game <name>` sample launch | ✔ | ✖ | Code-driven runtime samples have no authored scene, so there is nothing for a DCC tool to launch. |

## Known limitations

**Arbitrary world node trees.** A Sky Texture, HDRI, or node-group world cannot be evaluated
from Python without rendering. Those fall back to the world's viewport colour; for fidelity,
use the authored sky gradient, which also gives exact parity with a Godot-authored scene.

**Packed and generated images** cannot be referenced by the contract — the runtime loads
textures from files under `data/`. The exporter warns and omits the reference.

**Interaction collider geometry is not forwarded.** The contract's interactable component
carries only a display name today. Both hosts behave identically here.

**An object reference is baked as a NAME.** `authoredBy: entity` resolves to the target's `Name`
component, because that is the one thing every exported object carries. Names are not unique and
nothing here makes them so, so a scene with two objects sharing a name has an ambiguous reference
— the runtime resolving it is what must say so.

**Schema v5 is a hard break.** The engine refuses a v4 document by version rather than reading it:
a v4 entity is a JSON object where v5 expects an array, so it would parse as nothing and load an
empty scene with no diagnostic. There is no migration script in this repo — re-author and
re-export.

## Verification

Parity is not asserted by inspection. Three mechanisms enforce it:

1. **`contract-check`** round-trips every exported document through the engine's own
   `ExportJsonReader`/`ExportJsonWriter`. Anything the Python writer got wrong — a misspelled
   key, an enum written as a number, a missing field — shows up as a difference.
2. **Shared defaults.** `authoring/defaults.py` mirrors `ParadiseAuthoringDefaults`, so
   agent data cannot silently diverge between hosts.
3. **Shared math.** `collider_fold.py`, `layers.py`, `sky.py`, and the matrix layout are
   direct ports with unit tests pinned to the C# behaviour, including the half-away-from-zero
   rounding in `Color32.ToByte` that Python's built-in `round` would get wrong.
