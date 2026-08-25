# Conventions — Blender ↔ the Paradise export contract

The contract is defined by C# `Paradise.Export` and pinned by `ParadiseEngine/CONVENTIONS.md`
and the golden fixtures in `Paradise.Export.Test`. This document covers only what is *specific
to the Blender host*: where Blender's conventions differ from the contract's, and what this
addon does about it.

The Godot host has an easy job here — Godot's conventions **are** the contract's, so it writes
values verbatim. Blender's are not, in four places.

---

## 1. Handedness — the contract is Y-up, Blender is Z-up

Both are right-handed. The contract is **Y-up, −Z forward, +X right** (glTF/Godot); Blender is
**Z-up, −Y forward, +X right**. The conversion is a rotation of −90° about X:

```
C = Rotation(-90°, X)        C·(x, y, z) = (x, z, -y)
```

Transforms are rebased by **conjugation**, not left-multiplication:

```
M_contract = C · M_blender · C⁻¹
```

Left-multiplying alone moves an object correctly but leaves its local axes in the old basis, so
it breaks the moment transforms compose — i.e. any parent/child hierarchy. Conjugation is a
similarity transform and distributes over multiplication, which is exactly the property a
hierarchy needs.

Implementation: `paradise_blender/contract/axes.py`. Validation:

- `tests/unit/test_axes.py` proves the algebraic properties (composition, determinant, that the
  quaternion and matrix paths agree).
- `tests/integration/test_axis_parity.py` proves the conversion **matches Blender's own glTF
  exporter** with `export_yup=True`, across six transforms including non-uniform scale.

That second test is the one that matters. `RenderableComponentData.Mesh` points at a GLB the
glTF exporter wrote; if our node transforms disagreed with its vertex data, every mesh in every
scene would be rotated 90°, and the unit tests would still pass because they only check our
conversion against itself.

**Consequences worth internalising:**

| Blender | contract |
|---|---|
| `+Z` (up) | `+Y` |
| `−Y` (forward) | `−Z` |
| position `(x, y, z)` | `(x, z, −y)` |
| scale `(x, y, z)` | `(x, z, y)` — magnitudes, so no sign flip |
| triangle winding | **unchanged** — `C` is a proper rotation (det +1), so it cannot mirror |

Scale is the trap. It is tempting to decompose a Blender matrix and convert position, rotation,
and scale separately; that gets scale wrong, because the basis change permutes the axes. So
`export/transform.py` **converts the matrix first and decomposes second**, always.

## 2. Colour space — Blender is already linear

The contract stores **linear** colour, packed to 8 bits per channel (`Color32`).

The Godot host calls `Color.SrgbToLinear()` on every authored colour, because Godot stores
authored colours as sRGB. **Blender does not, and this addon must not.** Blender's socket
`default_value` colours are linear scene-referred floats — the colour picker only *displays*
them through a view transform. Applying the transfer function here would darken every material
in the scene by roughly the gamma curve, and it would look like a plausible lighting difference
rather than a bug.

The exception is anything authored through a widget that means sRGB by convention: the
procedural-recipe tints and fog colour in `authoring/material_props.py` and
`authoring/world_props.py`, which mirror Godot metadata and are documented as sRGB. Those are
linearized explicitly, and each call site says so.

## 3. Numbers — the contract is float32

The contract's floats are C# `float`, rendered by System.Text.Json with shortest-round-trip
precision **for 32 bits**. A Python double renders `8/255` as `0.03137254901960784`; the
contract renders `0.03137255`.

`contract/writer.py`'s `f32_repr` quantizes through `struct.pack('f', …)` and picks the
shortest `%.{p}g` that round-trips as float32. With it, exported documents are **byte-identical**
to what the C# writer produces (verified by `contract-check`, which reports no textual
difference).

Note the contract is formally **value-based, not byte-based** — `5` and `5.0` are the same
value — so `contract-check` compares semantically and treats byte parity as a bonus.

## 4. Entity identity — Blender duplicates carry the GUID

`LevelEntityData.EntityGuid` must be stable per placement and unique per scene.

Godot stores it in node metadata and sweeps for collisions on editor save. Blender makes the
uniqueness half harder: duplicating an object (`Shift+D`, `Alt+D`, copy/paste) deep-copies its
property groups, GUID included, so a duplicate arrives **already colliding**, silently, and
there is no per-object "was duplicated" callback to hook.

So `authoring/guid.py` mints lazily and sweeps on `save_pre`, and the exporter sweeps again
before walking the scene (so an unsaved .blend still exports unique identities). Collision
resolution keeps the first object in a **stable name ordering** and re-mints the rest —
without a deterministic order, which duplicate keeps the original GUID would vary between runs.

---

## Deliberate deviations from the Godot host

These are cases where matching Godot exactly would have been worse.

### Local transforms are relative to the parent *entity*

Godot writes a node's own `Position` while reporting the nearest `EntityExport` ancestor as
`Parent` — so when a plain `Node3D` sits between two entities, the local transform and the
declared parent disagree. Blender scenes routinely have such intermediates (empties used for
grouping or rigging), so `export/entity.py` computes the local transform relative to the
declared parent entity. For the common case where the parent entity *is* the immediate parent,
the two agree exactly.

### Entity order is sorted by name

Godot walks its scene tree depth-first, giving a stable order for free. Blender guarantees no
iteration order for `scene.objects`, so entities are emitted sorted by name. Without that, two
exports of an unchanged scene could differ, making every diff and every live-preview patch noisy.

### Blender's own physics and rigid-body settings are not read

They describe a different solver. Its collision shapes (`CONVEX_HULL`, `MESH`) mostly have no
contract equivalent, and an object can carry Blender physics purely for animation baking without
being a runtime dynamic body. Colliders and body type are authored explicitly instead.

### Spot cone angle is not doubled

Godot's `SpotAngle` is a half-angle, so its exporter doubles it. Blender's `spot_size` is
already the full cone angle. Same contract value, different arithmetic — an easy thing to
"fix" into a bug while porting.

---

## Approximations, stated plainly

Two conversions have no correct answer, only a defensible one. Both are constants with the
reasoning at their definition.

**Light intensity** (`export/light.py`). Blender measures point/spot/area output in watts;
the contract carries a unitless multiplier in Godot's convention. There is no physically
correct conversion without also fixing an exposure model, so `WATTS_PER_INTENSITY_UNIT = 100`
maps Blender's default lamp to the contract's default light — defaults match, and scaling from
there is predictable.

**Area lights.** Neither the contract nor Godot has an area light type. They export as point
lights with their dimensions recorded in `AreaSize`, and the exporter warns. Dropping them
would make a scene go dark with no explanation.

---

## What Blender cannot express, and is authored instead

`EnvironmentData` is shaped around Godot's `Environment`: a two-part procedural sky gradient,
SSAO, glow, fog. Blender's world is an arbitrary shader node tree that cannot be evaluated from
Python without rendering, and its AO/bloom settings moved between EEVEE versions.

So `authoring/world_props.py` authors those explicitly, mirroring Godot's `ProceduralSkyMaterial`
field for field — which is what makes cross-host parity achievable. The parts Blender *does*
express reliably are read from it and are deliberately absent from that group:

| contract field | Blender source |
|---|---|
| `TonemapMode` | `scene.view_settings.view_transform` (Standard→Linear, Filmic→Filmic, AgX→Agx, ACES→Aces) |
| `TonemapExposure` | `2 ^ scene.view_settings.exposure` (Blender's is in stops) |
| `BackgroundColor` | the world's Background node colour × strength |
| sun direction/colour/energy | the scene's first visible sun lamp |

Ambient is **computed, not copied**: `contract/sky.py` integrates the chosen sky over the
hemisphere to produce the three zone colours and the L2 spherical-harmonic coefficients, using
the same math the Godot host uses. The integral returns `E/π` — the cosine-weighted average
radiance, which is what the engine consumes directly. An extra π makes every scene π times too
bright, which is why `tests/unit/test_sky.py` pins it.

## 5. Host-object references — authored as a reference, exported as a value

Some `[Authored]` fields are not typed in: they point at one of Blender's own objects, and the
exporter bakes what that object IS into the field's own numbers. The engine calls this
`[AuthoredByHost(kind)]`, and the kinds are a closed set (`Paradise.Authoring`'s
`AuthoredBySources`): shape, mesh, sprite, light, asset, **transform**.

**This host implements exactly one of them: `transform`.** The others are still reported in the
Components panel as *"baked from …; not authored in Blender yet"* — colliders, meshes and lights
reach the export through their own dedicated Blender-native paths instead, not through this
mechanism.

A `transform` reference is an **object slot**: you pick an object, and you place it with Blender's
own move/rotate gizmo. That is the entire point — a destination you can see is a destination you
can place correctly, where three floats in a panel are numbers nobody can check without running
the game.

Three rules, each of which is silent when broken:

- **The store holds a NAME, the wire holds a POSE.** `obj["paradise:<id>/<Path>"]` is the
  referenced object's name; nothing mirrors its transform into the panel, because a second copy of
  the numbers is a copy that can disagree with the object. The pose is read at export, so *moving
  the target is the whole edit*.
- **Baked BY FIELD NAME**, filling whichever of `Position` (vector3), `Rotation` (quaternion),
  `Yaw` (float) and `Scale` (vector3) the record declares, ignoring everything else. That keeps
  this host general: it never learns what any particular record means by a pose. `Yaw` is
  `atan2` over the rotated +Z — the same convention the runtime reads a heading with, so a Z
  rotation in Blender and an actor's heading are the same number rather than two that look alike.
- **The rebase is §1's, reused.** `export.transform.decompose_contract` does the Z-up → Y-up
  conversion for this exactly as it does for every other transform; a second copy of the basis
  change is how the two silently disagree about which way is up.

A reference resolves against the FILE, not the scene (`bpy.data.objects`), so an object living
only in another scene of the same .blend still bakes. That is deliberate — what a pose reference
points at is a place, not something the document has to contain — but it does mean a target you
cannot see in the current scene is not an error.

An unassigned, dangling or self-referencing slot bakes **nothing**, so the payload carries the
record's own defaults and the runtime sees the field unauthored. Warned, never guessed: the world
origin is a real place, and a silently-zeroed destination is indistinguishable from one somebody
meant. Whether that is acceptable is the *game's* decision — ShiningPie's trigger volumes refuse an
unset destination at load — and it can only make it if the export is honest.

A reference may be **nested inside an ordinary composed field** — the engine reads `authoredBy` at
every depth — so its path is multi-segment (`Container/Destination`). The leaf schemas therefore
travel ON the `HostRef`, captured during the walk; re-deriving them afterwards from the path is
what dropped a nested reference's whole payload once.

`tests/unit/test_authoring.py::TestTransformReferences` pins the schema and payload halves,
nesting included; `tests/integration/test_authored_components.py` pins the bake, the rebase and
the three refusals.
