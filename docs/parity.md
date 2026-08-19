# Parity with the Godot addon

Feature-by-feature comparison with `ParadiseGodotEditor/addons/paradise/`, which is the
reference implementation of the export contract.

Legend: **✔** full parity · **≈** parity with a documented difference · **✖** not implemented

## Scene export

| contract area | Godot host | Blender host | notes |
|---|---|---|---|
| Entity transforms | ✔ verbatim | ≈ | Blender is Z-up, so transforms are rebased (`CONVENTIONS.md` §1). Pinned against Blender's glTF exporter. |
| Entity identity (GUID) | ✔ | ≈ | Same lifecycle; Blender duplicates carry the GUID, so the collision sweep matters more (§4). |
| Parent/child | ✔ | ≈ | Local transform is relative to the parent *entity*, not the immediate object — deliberate. |
| Entity order | tree order | sorted by name | Blender guarantees no iteration order; sorting keeps exports diff-stable. |
| Mesh reference (`Renderable`) | ✔ shared GLB | ✔ | Exported per mesh datablock, deduplicated. |
| Materials | ✔ `BaseMaterial3D` | ≈ | Principled BSDF. Colour space is inverted (§2) — Blender is already linear. |
| Material slot order | ✔ | ✔ | Slot order, matching the GLB's primitive order. |
| Procedural recipes, transmission | ✔ resource metadata | ✔ | Property group on the material. |
| Colliders (box/sphere/capsule) | ✔ typed shapes | ≈ | Blender has no collision primitives; shape is marked and dimensions come from bounds or explicit values. |
| Collider scale folding | ✔ | ✔ | Same `ColliderScaleFold` rules, ported. |
| Collision layers | ✔ mask → index | ≈ | Authored as an index directly, making the lossy multi-layer case unrepresentable rather than merely warned about. |
| Rigidbody | ✔ | ✔ | Authored `paradise.rigidbody` wins; a derived body (static, or kinematic with an agent) is emitted alongside colliders. |
| Agent | ✔ | ✔ | The authored `paradise.agent` component, schema-driven in both hosts. |
| Sprite animation | ✔ from `Sprite3D` | ✖ | The schema marks the whole component host-baked (`authoredBy: sprite`); Blender has no sprite host object yet, so it is not authorable here. |
| Particle emitter | ✔ | ≈ | Authored `paradise.particle-emitter` component; the `Sheet` asset reference is host-baked and not baked here yet. |
| Prefabs | ✔ `PackedScene` | ≈ | Collection instances. `PrefabGuid` for a *local* collection derives from its name — Blender has no persistent datablock id (see below). |
| Prefab overrides | ✖ (no Godot API) | ✖ (not expressible) | Both emit `PrefabOverrideData` empty. |
| Camera | ✔ | ≈ | `ortho_scale` is a full span, halved to the contract's half-height. |
| Lights | ✔ | ≈ | Watts → multiplier is a calibration constant; area lights export as point lights. Spot angle is not doubled. A lamp marked as an entity exports as that entity's `Components.Light` and leaves the scene-level list. |
| Environment / tone mapping | ✔ Godot `Environment` | ≈ | Tone mapping and background read from Blender; sky gradient, SSAO, glow, fog authored explicitly (§"What Blender cannot express"). |
| Ambient SH | ✔ | ✔ | Same integration and L2 projection code, ported. |
| Navmesh | ✔ `NavigationServer3D` | ✔ | Baked by `tools/ParadiseBlenderBridge` with DotRecast — the same library, same parameters, same binary. |
| `ProjectSettings.json` | ✔ | ✔ | Contract defaults; no Blender counterpart for any field. |
| KTX2 texture pipeline | ✔ | ✔ | Same `toktx` invocation. |
| UI asset staging | ✔ | ✖ | The Godot host stages `res://ui/**` into `data/ui/`. Noesis XAML is authored in Noesis Studio, not Blender, so there is nothing for this host to stage. |
| Authored components (`Components.Custom`) | ✔ `AuthoredEntityNode` | ≈ | Same `authoring-schema.json`, same wire format, hot-reloaded on game rebuild. Host-object references (`authoredBy`: shape/node/sprite/asset baking) are not authored in Blender yet — those fields export absent, which the reader treats as unauthored. |
| Engine components via schema | ✔ | ≈ | Identity, agent, rigidbody, audio and particles are schema-authored and routed to their typed slots (`contract/authoring_router.py`); renderable, colliders, interactable and lights stay derived from real Blender data. The fixed per-field entity mirror is gone. |

## Workflow

| | Godot host | Blender host |
|---|---|---|
| Export on save | ✔ | ✔ |
| Manual export | ✔ | ✔ |
| Play in the .NET runtime | ✔ | ✔ |
| **Live preview** | ✖ | ✔ *(Blender side; needs the engine listener — see `live-preview.md`)* |
| Entity visible in the outliner | ✔ (node type) | ✖ (a flag; panel + *Select Paradise Entities* compensate) |
| Runtime host auto-detection | ✔ | ✔ same order |
| `--game <name>` sample launch | ✔ | ✖ | Code-driven runtime samples have no authored scene, so there is nothing for a DCC tool to launch. |

## Known limitations

**Prefab GUID for local collections.** Blender has no persistent per-datablock id —
`session_uid` is reassigned on file load. A *linked* collection uses its library path, which is
genuinely stable; a *local* one uses its name, so renaming a prefab collection changes its
guid. There is no better handle available.

**Arbitrary world node trees.** A Sky Texture, HDRI, or node-group world cannot be evaluated
from Python without rendering. Those fall back to the world's viewport colour; for fidelity,
use the authored sky gradient, which also gives exact parity with a Godot-authored scene.

**Packed and generated images** cannot be referenced by the contract — the runtime loads
textures from files under `data/`. The exporter warns and omits the reference.

**Interaction collider geometry is not forwarded.** The contract's interactable component
carries only a display name today. Both hosts behave identically here.

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
