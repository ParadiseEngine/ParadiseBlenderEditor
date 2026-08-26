# Live preview

Keep a `paradise-runtime` window open beside Blender and have it follow your edits: move an
object in the viewport, see it move in the engine, with the engine's real renderer and real
simulation.

```
┌─ Blender ────────┐         ┌─ paradise-runtime ─┐
│ Paradise tab     │         │  SDL window        │
│  ▸ Play          │────────▶│  PBR renderer      │
│  ▸ Live Preview ●│  ▲      │  real simulation   │
└──────────────────┘  │      └────────────────────┘
      depsgraph ──────┘  TCP loopback, NDJSON deltas
```

## Status — read this first

**Implemented and verified against a real game: `ShiningPie.Launcher --live`.**

The listener lives in the ShiningPie repository (`ShiningPie.Launcher/LiveLink.cs` and
`LiveSceneApplier.cs`). Point the addon's runtime host at that launcher, or start it yourself:

```bash
cd ShiningPie && dotnet run --project ShiningPie.Launcher -- --live   # listens on 45123
```

Then press **Start Live Preview**. Moving an object in Blender moves it in the running game.

`paradise-runtime` (the engine sample) still has **no** `--live` listener — that would be a
separate change in `ParadiseGodotEditor`. Against that host the button still waits 60s and
reports the absence.

`tools/mock_runtime.py` remains the executable spec and is what
`tests/integration/test_live_preview.py` drives, so the Blender side stays testable with no game
present.

To try it now:

```bash
python3 tools/mock_runtime.py --verbose        # terminal 1
blender --background --factory-startup --python tests/integration/test_live_preview.py
```

## Protocol

Newline-delimited JSON over a loopback TCP socket. **Blender is the client; the runtime
listens.** That direction is deliberate: the runtime already owns a window and an event loop,
so making it the server lets Blender connect, drop, and reconnect — across an addon reload,
say — without the preview window dying.

NDJSON rather than a binary format because the payloads are already the contract's JSON, the
volume is tiny (tens of entities at ~10 Hz), and being able to `nc` the port and read what
Blender is sending is worth more here than bytes on the wire.

Every message is a JSON object with a `type` and a monotonically increasing `seq`.

```
Blender                                   runtime
   │── hello ────────────────────────────▶│  version handshake
   │◀─ ready ─────────────────────────────│  (or `error`, and close)
   │── scene/full ───────────────────────▶│  whole LevelData
   │── scene/patch ──────────────────────▶│  changed entities only
   │── scene/patch ──────────────────────▶│
   │── asset/invalidate ─────────────────▶│  a GLB or material was rewritten
   │── scene/full ───────────────────────▶│  after a structural change
   │── bye ──────────────────────────────▶│
```

### Messages

| type | payload | sent when |
|---|---|---|
| `hello` | `protocol`, `scene`, `dataDir` | on connect. `dataDir` lets the runtime resolve the mesh and material paths the payloads reference |
| `scene/full` | `scene`: a complete `LevelData` | at handshake, and after any structural change |
| `scene/patch` | `updated[]`, `added[]` (whole objects — component arrays), `removed[]` (object names) | transform or property edits |
| `asset/invalidate` | `fields[]`: data-relative contract paths | after a GLB or material is rewritten |
| `bye` | — | preview stopped cleanly |

**Patches carry whole entity documents, not field diffs.** An entity document is small, and a
field-level diff would require the runtime to implement merge semantics for every contract
field — far more surface for the two sides to disagree on. `removed` carries GUIDs because
names are not stable.

**Sequence numbers exist so the runtime can detect a gap.** The sender's queue is bounded and
drops the oldest pending messages when the runtime stops keeping up (see below), so a gap is a
real possibility. On noticing one, the runtime should ask for a resync rather than render a
scene that has silently diverged.

### Full resync vs patch

The Blender side chooses:

| change | message |
|---|---|
| an entity's transform or properties | `scene/patch` |
| an entity added, removed, or unmarked | `scene/full` |
| a light, the camera, the world, or a material | `scene/full` |

Lights and the camera live in the level document's header, outside the entity list — there is
no patch vocabulary for them, and a partial update would leave the runtime's scene out of step.

## How the Blender side stays responsive

Two problems, both solved structurally rather than by tuning.

**Rate.** `depsgraph_update_post` fires on essentially every mouse-move during a drag —
hundreds per second. The handler therefore does almost nothing: it records *which* objects are
dirty in a set and returns. A timer drains that set at the configured rate (default 10 Hz) and
sends one coalesced patch. Dragging for a second produces ~10 messages instead of ~500, and the
last one always carries the final position.

**Blocking.** Blender's UI is single-threaded, so a blocking `send` on a socket whose peer has
stalled would freeze the editor — and the peer is a game runtime that legitimately stalls for a
frame while it loads a mesh. So sends go onto a bounded queue drained by a background thread;
the main thread only ever appends.

The queue is bounded at 256 messages. If the runtime stops draining, an unbounded queue would
grow until Blender ran out of memory; instead the oldest pending messages are dropped and the
count is reported when the session ends. Dropping is safe for this protocol because a later
`scene/full` supersedes everything before it — and the sequence numbers let the runtime notice.

Assets are **not** re-exported during a preview: rewriting every GLB on each transform tweak
would stall Blender for seconds, and the runtime already has the meshes loaded.

## What a new engine-side listener needs

`ShiningPie.Launcher` is the reference implementation; the sketch below is what any other host
would reproduce:

1. `--live <port>` in `Paradise.Sample.Runtime/Program.cs`, alongside the existing `--scene`.
2. A `LiveLinkListener`: accept one client, validate `protocol == 1`, reply `ready`, and read
   NDJSON lines on a background thread into a concurrent queue.
3. Between simulation ticks, drain the queue and apply:
   - `scene/full` → rebuild the world via the existing `SceneAssembler` path
   - `scene/patch` → look up each entity by GUID and update its transform/components; spawn
     for `added`, despawn for `removed`
   - `asset/invalidate` → drop the named mesh/material from the asset cache
4. On a sequence gap, log it and (optionally) request a resync.

`tools/mock_runtime.py` is the executable specification — its scene-dictionary behaviour is
what the C# listener should reproduce.

## Configuration

Addon preferences:

| setting | default | notes |
|---|---|---|
| Live Preview Port | 45123 | loopback only |
| Live Update Rate | 10 Hz | lower it if the runtime cannot keep up; the session reports dropped messages on stop |
