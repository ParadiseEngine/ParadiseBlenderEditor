# Conventions — Blender ↔ the Paradise document format

The format is defined by C# (`Paradise.Assets.Documents`, `Paradise.Export`) and pinned by
`ParadiseEngine/CONVENTIONS.md` and the fixtures in `Paradise.Assets.Documents.Test`. This
document covers only what is *specific to the Blender host*: where Blender's conventions differ
from the document's, and what this addon does about it.

The Godot host has an easy job here — Godot's conventions **are** the format's, so it reads and
writes values verbatim. Blender's are not, in four places, and every one of them fails silently
rather than loudly.

---

## 1. Handedness — the document is Y-up, Blender is Z-up

Both are right-handed. The document is **Y-up, −Z forward, +X right** (glTF/Godot); Blender is
**Z-up, −Y forward, +X right**. The conversion is a rotation of −90° about X:

```
C = Rotation(-90°, X)        C·(x, y, z) = (x, z, -y)
```

Transforms are rebased by **conjugation**, not left-multiplication:

```
M_document = C · M_blender · C⁻¹
```

Left-multiplying alone moves an object correctly but leaves its local axes in the old basis, so
it breaks the moment transforms compose — i.e. any parent/child hierarchy. Conjugation is a
similarity transform and distributes over multiplication, which is exactly the property a
hierarchy needs.

Implementation: `paradise_assets/document/axes.py`. Validation:

- `tests/unit/test_assets_axes.py` proves the algebraic properties (composition distributes over
  conjugation, the two directions are inverses, a half turn decomposes precisely).
- `tests/integration/test_axis_parity.py` proves the conversion **matches Blender's own glTF
  exporter** with `export_yup=True`, across seven transforms including non-uniform scale.

That second test is the one that matters. A document's `transform` places a mesh that lives in a
GLB somebody's glTF exporter wrote; if the two disagreed, every mesh in every document would be
rotated 90°, and the unit tests would still pass because they only check our conversion against
itself.

**Consequences worth internalising:**

| Blender | document |
|---|---|
| `+Z` (up) | `+Y` |
| position `(x, y, z)` | `(x, z, −y)` |
| scale `(x, y, z)` | `(x, z, y)` — magnitudes, so no sign flip |
| triangle winding | **unchanged** — `C` is a proper rotation (det +1), so it cannot mirror |

Scale is the trap. It is tempting to decompose a Blender matrix and convert position, rotation,
and scale separately; that gets scale wrong, because the basis change permutes the axes. So
`axes.py` **converts the matrix first and decomposes second**, always.

Two smaller traps live in the same file, both paid for on ShiningPie:

- **Normalize a document quaternion before composing it.** They are float32-quantized, so none is
  exactly unit; the length error leaks through the rotation matrix and comes back out of the
  decompose as SCALE. A stored `20.0` became `19.999998` on the skyline props.
- **Decompose with Shepperd's method.** The naive w-first formula divides by `sqrt(1 + trace)`,
  which is zero for a 180° rotation — and an axis-aligned scene is full of them.

## 2. Numbers — the document is float32, the text is `repr`

The document's floats are C# `float`. The canonical writer emits the shortest digits that
round-trip, formatted by **Python's `repr` rules** — the C# `CanonicalTomlWriter` adopted those
rules so this side could be one call to `repr`. Do not reimplement it, and do not "improve" the
formatting: `paradise assets prefab-check` compares BYTES, so a formatting difference is a failing
check on every document this addon has touched, not a style nit.

`document/canonical_toml.py` is that writer, and `tests/unit/test_parity_corpus.py` re-emits a
corpus the C# writer produced and demands byte equality.

**An object nobody moved must keep its authored numbers verbatim.** Documents store values that
came from C# `float`, Blender stores float32, and the axis rebase runs a square root — the round
trip is accurate to about 4e-8 relative, which is fine as a position and fatal as text, because
`repr` of a value that moved in its last bit is a completely different string. `save._unchanged`
compares against `_EPSILON = 1e-6`, relative to the stored magnitude (400 m and 0.01 scale do not
deserve the same slack) and by dot product for rotations (q and −q are one rotation). Its epsilon
is not tuning: below it, merely loading a document would churn it.

## 3. Identity — read from a sidecar, never minted here

Every file under `assets/` has an identity in `<file>.meta`, and **this addon does not write
one**. `paradise assets watch` runs the C# `SidecarMaintainer`, which mints a sidecar for any
file lacking one; two minters race and the loser's guid is dropped with a `Conflicted` log line.
So `document/sidecar.py` has `read` and `wait_for` and deliberately no `write`, and creating a
prefab has a *prerequisite* rather than an ordering rule: no watcher, no identity, no new prefab.

Object identity inside a document is the document's own `meta.Guid`, carried on the Blender
object as an ID property (`materialize/store.py`). It is stored, not derived — which is the
opposite of what the `.blend`-is-truth exporter did, and the reason renaming an object here is
free.

**Create Prefab from Selection** snapshots raw static meshes into a GLB, then lets the CLI
extract canonical mesh/material/prefab documents and mint their sidecars. If extraction routes
the prefab elsewhere, `paradise assets mv` moves the seed and its identity to the chosen path.
The Blender scene is unchanged. **Create Prefab from Object** instead extracts an existing
document subtree and leaves an instance. In either workflow, save the level containing the
instance and build assets before playing. Keep `.meta` files with their assets; a linked `.blend`
library and name-derived GUIDs are not part of this workflow.

## 4. Names — Blender's namespace is not the document's

Blender guarantees object names are unique within a file and silently uniquifies to get there
(`Wall` → `Wall.001`), truncating at 63 bytes, in one namespace shared with every node of every
imported GLB. A document allows two objects one name and has no length limit.

So `obj.name` alone cannot say whether the AUTHOR renamed anything. `store.tag_name` records both
the document's `meta.Name` and the name Blender showed at load; `store.document_name` returns the
author's rename when the shown name still matches, and the authored name untouched otherwise
(#32). Without that, opening and saving a level renamed half of it to Blender's spellings.

---

## What this host does NOT do

Worth stating, because the sibling exporter that did all of it was in this repository until
recently and its habits are easy to reintroduce.

**It does not rebuild components.** `save.py` takes payloads from the RE-READ document, not from
Blender. That single decision is what lets a document full of components this addon has never
heard of be opened and saved without corruption. Editing a field does not change it: `edits.py`
holds an overlay of only the members someone actually touched, applied over the file's version at
merge time.

**Saving a prefab does not read Blender's materials, lights, cameras or physics.** Those belong to the
document, which the CLI compiles; the `.blend` is a cache and anything read out of it would be a
second source for a value that already has one. The one exception is display: `load.py` reads a
material document's `BaseColorFactor` into `obj.color` so an untextured instance is not grey.

Explicit creation from raw geometry is a one-time import: Blender's glTF exporter snapshots
evaluated meshes and supported materials, with matrices baked into vertices relative to the
active object's world origin. Baking retains shear from nonuniformly scaled parents; mirrored
geometry reverses winding. Later prefab saves read canonical documents, never the source meshes.

**It does not convert colour.** The exporter had a whole rule here (Blender's socket colours are
already linear; do not `srgb_to_linear` them). This addon authors no colour at all.

**It does not derive identity from names** — see §3. That was the exporter's bargain, made because
writing a guid into the `.blend` would have dirtied a Git-LFS-locked binary on every export. There
is no export, so there is no bargain.
