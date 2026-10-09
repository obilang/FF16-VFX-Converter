# FF16 `.vfxb` (Visual Effect Binary) — Format Notes

Reverse-engineering notes for Final Fantasy XVI visual-effect files (`*.vfxb`),
as understood while building [vfxb_decode.py](vfxb_decode.py) and
[vfxb_viewer.py](vfxb_viewer.py). Based on the 010 Editor template in
[VFX/vfxb.bt](VFX/vfxb.bt) plus byte-level verification against the 55 phoenix
summon effects in
`E:/Workspace/FF16/Output/vfx/chara/pc/summon_act/phoenix/ve/`.

> **Confidence key** — 🟢 verified against raw bytes across files · 🟡 strong
> inference · 🔴 guess / unverified. Treat 🔴 as "do not rely on."

---

## 1. What a `.vfxb` is

A `.vfxb` is an authored **particle / visual-effect graph**, conceptually like
Unreal's **Cascade / Niagara** (system → emitters → modules), *not* like a
material-function call graph. It is a **containment tree** (each node has one
parent), not a DAG of reusable subgraphs.

Every effect is a tree of **Item nodes**. Each node has:
- a 32-bit **type hash** (its "what am I" id),
- a **child count**,
- a list of typed **properties** (scalars, animation curves, texture refs, …).

Container nodes (depth 0–1) mostly just group children and carry no curves; the
actual animated work lives in the **leaf nodes** (the "modules").

Typical scale (across the 55 sample files): 🟢
- 767 total graph nodes, up to **5 levels deep**, ~14,000 keyframes total.
- Depth 2 is the common case: `system → emitter → behaviour modules`.

---

## 2. Top-level file layout

```
0x000  Header (magic "VFXB", counts, data-section sizes)
0x030  root_items[15]         (ItemEntry, 0x10 each; offset==1 => empty slot)
0x120  unknown_data[0x98]
0x1B8  blob pointer table     (shader / string / blob_0x2 / item blob + 14 sub-offsets)
....   shader blob            ("TEC" container — embedded DXBC shaders)
....   string blob            (NUL-terminated asset paths: .mdl / .tex)
....   item blob              (properties, items, constants, texture tables, …)
```

**Root items = top-level timelines / stages.** The 15 `root_items` slots are the
effect's top-level nodes; `offset == 1` marks an empty slot. A file can populate
several roots for distinct "stages" of an effect — e.g.
`a00s_fire_buil_scaffold04_y` (a burning building) has **4 root items = 4
stages**, but only one (`0x31C9ED44`, dur 60, 6 spawn edges) is authored; the
other three are **empty placeholders** (dur 1, no children, no spawn edges, just
a few marker props like `0x3B`/`0x25`/`0x27`). So "3 of 4 timelines empty" is
expected — the stages exist as slots but were never filled in.

### Header fields (offsets from 0) 🟢
| off | type | meaning |
|-----|------|---------|
| 0x00 | char[4] | magic `VFXB` |
| 0x08 | int | property_data_size |
| 0x0C | int | item_data_size |
| 0x10 | int | total_data_size |
| 0x18 | int | vertex_set_count |
| 0x1C | byte | texture_count |
| 0x1D | byte | constant_count |
| 0x1F | byte | struct_0x2_count |
| 0x20 | byte | string_count |

### Blob pointer table (@ 0x1B8) 🟢
`shader_blob{addr,size}`, `string_blob{addr,…}`, `blob_0x2{addr,…}`, then the
**item blob**: a base address followed by 14 signed sub-offsets. The ones that
matter:

| sub-offset | section |
|-----------|---------|
| 0 | properties |
| 1 | items (the node array) |
| 2 | constants |
| 4 | constant_data |
| 7 | vertex_set |
| 8 | texture_indices |
| 9 | project_info |
| 13 | texture_groups |

---

## 3. Item (node) structure 🟢

Each node is a fixed **0x7C-byte header** followed by its property block.

| off | field |
|-----|-------|
| 0x00 | field_0x0 → float pointer (`address_0x0 = properties_addr + field_0x0`) |
| 0x08 | packed: `property_size` (low 24 bits) + `property_offset` (high 8 bits) |
| 0x0C | float **duration** — effective lifetime / timeline bar length 🟢 |
| 0x10 | float **lifetime_base** — base/min lifetime (== duration when no range) 🟢 |
| 0x14 | float **lifetime_range** — "lifetime range" editor field (0 when unused) 🟢 |
| 0x38 | child_count |
| 0x70 | **hash** (uint32 type id) |

The property block starts at `node + 0x7C + property_offset` and runs for
`property_size` bytes. Child nodes are reached through `0x2B` (kItem) properties
(see below).

### Timeline / lifetime (0x0C / 0x10 / 0x14) 🟢

The SE VFX editor draws each node as a **bar on a timeline** (root spans the
whole effect; emitters and particles are nested bars). There is **no dedicated
timeline structure** in the file — unlike `.tlb` — the bars are reconstructed
from the Item tree plus these three header floats:

- **`duration` (0x0C)** is the bar **length** — the editor's "lifetime". Confirmed
  against three known-answer files (bar lengths read off the editor matched
  exactly): e.g. `sm_skl_upper_firepool_01s` root=31, emitter=31, children
  14/26/11/14; `ramu_skl4_thunder_atk_01s` root=123, emitter=60, children
  10/5/5/12.
- **`lifetime_base` (0x10)** and **`lifetime_range` (0x14)** are the editor's
  "lifetime" + "lifetime range" inputs (the range's dropdown defaults to "plus").
  Usually `0x10 == 0x0C` and `0x14 == 0`, but ~1 node in 6 carries a real range
  (e.g. base=21, range=6, duration=24), so these are **distinct fields, not a
  mirror** of duration.

**Bar START is NOT stored in the item — it is derived at runtime.** Two hard
proofs:
1. In `ramu`, sibling particles at start 0 vs 2 have **byte-identical** item
   records + property headers (only the param-block pointer and hash differ).
   Identical data at different timeline positions ⇒ start is not in the item.
2. In `firepool`, an emitter's 7 children have starts `[0,3,0,0,15,3,3]` (15 is
   very distinctive). That exact sequence does **not** appear anywhere in the
   file — searched float at all strides 4–64, and contiguous int32/int16/uint8.

Conclusion: the editor's start positions are computed from emitter spawn logic
(spawn rate / delay curves / emitter lifetime), not persisted per node. The
decoder therefore emits `duration` / `lifetime_base` / `lifetime_range` (each
with a `*_addr` absolute offset for in-place patching) but **no** start field.

**Where the playback start actually lives: the `.tlb`.** A `.vfxb` is a passive
asset; *when* it plays is set by the character-timeline file (`.tlb`, magic
`FCTL`, see `tlb_decode.py`) that references it. Timeline elements of type
`1023` / `1030` carry `data.vfx.path` (the `.vfxb`), a `frame_start` (the
playback start on the animation timeline), a `num_frames` (hold/loop length),
and `vfx_emit_params` (attach eid, offset, rotation, scale). Confirmed:
`…/sm_phoenix/sk01_phoenix_swing_up_01_origin.tlb` element with `field_0x00=107`
plays `sm_skl_upper_firepool_01s.vfxb` at `frame_start=9`. So the complete
timing model is:

| where | field | meaning |
|-------|-------|---------|
| `.tlb` element | `frame_start` | **when** the effect starts (timeline start) |
| `.tlb` element | `num_frames`  | how long the timeline holds/loops it |
| `.vfxb` item   | `duration` (0x0C) | the effect's own per-item bar length |

---

## 4. Properties

A property is `type:u16, size:i16, …payload…`, `size` bytes total. `size < 4`
terminates the block. ~90 distinct property types exist; only a handful are
decoded. Known ones:

| type | name | payload | conf |
|------|------|---------|------|
| `0x2B` | **kItem / spawn edge** | container→spawned(particle\|emitter) link + spawn schedule; see §4b | 🟢/🟡 |
| `0x8F` | keyed curve | indirect → KeyList | 🟢 |
| `0x91` | keyed curve | indirect → KeyList (variant) | 🟢 |
| `0x1E` | scalar | single indirect float; carries `target_offset` (see §5b) | 🟡 |
| `0x8A` | scalar2 | two indirect floats; carries `value_offsets` | 🟡 |
| `0x8B` | scalar2 | float + bound float; carries `target_offset` (see §5b) | 🟡 |
| `0x31` | **texture group** | see §6 — note it may carry *no* group (distortion pass) | 🟢 |
| `0x90` | model/mesh ref (?) | see §7 | 🔴 |
| others | generic | kept as raw hex + best-effort float/int | — |

> ⚠️ The `scalar`/`scalar2` value interpretations are byte-plausible but not
> semantically confirmed — some "float" slots are actually small ints / flags
> (e.g. `0x8A` first value often decodes to a denormal like `2.35e-38`, which is
> the byte pattern `0x01000000` — i.e. an int `1`, not a float).

---

## 4b. Spawn edges (`0x2B`) — the "timeline" 🟡

The graph is **not** emitter→particle by containment; it is a set of **spawn
edges**. A `0x2B` property on a node is one spawn connection: it names what to
spawn (a particle *or* another emitter — sub-emitters nest to any depth) and how
to spawn it. An "emitter" is really just a **container** that owns spawn edges;
what the SE editor draws as a *timeline* is the schedule carried on the edge.
(User-confirmed model; the editor shows spawn items as an independent node type,
each with its own detail panel: spawn mode / interval / count / max-at-once, plus
spawn type = mesh/vertex etc. for Niagara-style mesh-surface spawners.)

**0x20-byte edge header** (from `GenerateItemProperty` in `vfxb.bt`):

| off | field | meaning |
|-----|-------|---------|
| 0x00 | `type=0x2B`, `size=0x20` | |
| 0x04 | `count` (i32) | **spawn number** (burst size; e.g. 3 = spawn 3 at once) |
| 0x08 | `field_0x3` (VarOffset 30/2) | → `item_info[count]` at `addr_0x0 + off`; each 0x10-byte entry = `{i32 child_offset, f32 weight, Offset24, Offset24}` |
| 0x0C | `field_0x4:6` + `field_0x4a:10` | slot index + an unknown 10-bit field |
| 0x0E | `field_0x5` (i16) | unknown |
| 0x10 | `field_0x6` (Offset24) | → **spawn-interval curve** (set only when "use curve" is on) |
| 0x14 | `field_0x7` (Offset24) | → **spawn-count curve** (set only when "use curve" is on) |
| 0x18 | `field_0x8` (OffsetT) | props[f8] → OffsetT → props → float (usually 1.0) |
| 0x1C | `field_0x9` (OffsetT) | props[f9] → OffsetT → props → float (often 60.0 / a duration) |

**f6/f7 are curves, not scalars — gated by a "use curve" checkbox.** The SE
editor's spawn panel has a checkbox to *drive spawn interval / spawn count with
a curve* instead of a constant. That is exactly what f6/f7 are: `field_0x6` is
the **spawn-interval** curve and `field_0x7` is the **spawn-count** curve, each
present only when its checkbox is on. Evidence:

- Most edges have `f6 = f7 = -1` (checkbox off → a plain scalar interval/count
  is used, and the default is not stored at all).
- A spawn edge has **four independent param-curve pointers**: `f6` = spawn
  interval, `f7` = spawn count, `f8` and `f9` = two more params (meaning TBD).
  Any of them is `-1` when that param is left at its default (defaults aren't
  stored). f6 = interval is confirmed against ground truth: scaffold root edge 0
  f6 = **5.0**, matching its editor panel's spawn interval.
- Each pointer targets a run of **fixed 16-byte records**:
  `{i32 backptr, i32 tag, i32 subtype, f32 value}`. A record is a key while
  `tag & 0xFF == 1` (`0x201`/sub 28 or `0x101`/sub 21 — an interpolation flag,
  meaning TBD). No per-key tangent; the trailing float is the value.
- ⚠️ **The four runs are packed CONTIGUOUSLY, so a run ends at the NEXT-HIGHER of
  the four offsets** — not at the first non-key word. Walking past that boundary
  bleeds the following pointer's record into this curve. That bug produced bogus
  multi-key readings: the spark count looked like `[10, 1]` and interval like
  `[15, 1]`, but the trailing `1.0` was actually f8's own single-value record.
  With the boundary rule each param is a clean single value (curves with genuine
  multiple keyframes are still read in full).
- Confirmed single values: `pnx_wing_app_nml_01s` spark interval **0.3403**,
  count **10**; `pnx_wing_lost_nml_01s` spark interval **15**, count **not set**
  (uses default); scaffold `0x513FA4D0` interval **0.1**, count **30**; scaffold
  root edge 0 interval **5.0**. (The app/lost sparks are *variants*, not copies —
  different durations 40 vs 35 and different child curve counts — so their tuned
  interval/count values legitimately differ.)

**"Not overridden ⇒ not stored" (confirmed by the above).** The panel's
`spawn count = 100` / `max at same time = 100` / item `ID` (1787) do **not**
appear as literals anywhere outside the shader blob. The file stores only
*overrides* — when a param is left at its default (or driven by a constant the
editor treats as default), nothing is written. This is the same reason the
per-particle *start* is absent (§3): defaults are filled by the editor/runtime,
not persisted. This is why blind value-searches for "100" and start offsets fail.

The decoder surfaces **all** edge fields (`spawn_count`, `slot`, `field_0x4a`,
`field_0x5`, the four param curves `interval_curve` / `count_curve` / `f8_curve`
/ `f9_curve` with `primary` + `primary_addr`, `item_info[]`, and absolute
addresses for the header integers). The viewer's **"Spawn edges"** inspector
section resolves each edge's `child_offset` to the spawned node's hash and
renders it as a **clickable link that jumps to that node** in the graph (two
edges → the same hash = the same particle spawned twice), and shows the four
param values.

**Editable for in-game testing.** `vfxb_encode.build_node_fields` now emits an
editable "Spawn edge N" section per edge: the four curve values (`interval` /
`count` / `f8` / `f9`, float) plus the header integers `spawn_count (burst)`
(i32), `field_0x4a` and `field_0x5`. `field_0x4a` is a 10-bit field packed with
`slot` in a u16, so it uses a **bitfield format spec** `"H:6:10"` — `patch_scalar`
/ `parse_value` / `read_scalar` understand `"<base>:<shift>:<width>"` and
read-modify-write only those bits, preserving `slot`. These are exposed because
they are the prime suspects for **spawn mode / max-at-once** — flip them and
observe the effect in-game (the app vs lost sparks are byte-identical except
offsets, so any mode difference is either the same mode with different tuning, or
a non-stored default).

`field_0x4a` (upper 10 bits of the packed short at edge+0x0C) is still
unexplained: it varies widely (1, 3, 6, 12, 20, 24, 100, 240…) and does **not**
track spawn count — edges that spawn a single particle carry f4a = 6/12/20/24.
Not the burst count (that is the header `count`). `field_0x5` mirrors `f4a` on
some edges and is 0 on others; neither split cleanly maps to a spawn mode in the
static data. TBD — now testable in-game.

---

## 5. Animation curves (KeyList) 🟢

A `0x8F`/`0x91` property points (via two indirections through the properties
section) to a **KeyList**: `count:i32` followed by `count` keys.

Each key is `type:i32, time:f32, …value…`. **The `type` is a per-key
interpolation tag, not a per-curve kind** — a single curve routinely mixes
types. Verified by byte-alignment across all files:

| type | name | payload after `time` | bytes | meaning (🟡) |
|------|------|----------------------|-------|--------------|
| 0 | `k0` | value:f32 | +4 | stepped / constant hold |
| 1 | `k1` | value:f32 | +4 | linear (**always** the final key) |
| 3 | `kVector3` | value + tangent_in + tangent_out | +12 | value with tangent handles |
| 2 | `k2` | value + 2 tangents + **16-byte easing ramp** | +28 | custom easing curve |

Key facts (🟢, measured over 3,577 curves):
- **`vals[0]` is always the keyframe VALUE** for every type — 94% of curves form
  smooth authored value-series when read this way. So a curve is always
  scalar-plottable.
- The **last key is always `k1` with `time == +inf`** — a "hold last value
  forever" end-sentinel. In plots this is clamped to just past the last real key.
- `k0` appears only first/interior, never last.
- The `k2` "easing ramp" is **16 bytes that ramp 0→255** (e.g.
  `[0,19,37,54,…,236,255]`) — a quantised custom-ease LUT, *not* floats. The
  template's "k2 = Vector4+Vector3 (7 floats)" is a size-correct but
  content-wrong interpretation.

### Vectors & colours = groups of scalar curves 🟡
No single key packs a vec4. Instead, **N adjacent curves sharing identical
keyframe times form a vecN** (one track per channel). Distribution:

| group | count | meaning |
|-------|-------|---------|
| 1 | 1915 | scalar (size, alpha, rate …) |
| 2 | 180 | vec2 (UV …) |
| 3 | 420 | **vec3 — RGB colour or XYZ scale** |
| 4 | 8 | vec4 — RGBA |

Two vec3 storage patterns observed 🟢:
- **Uniform vector** (uniform scale): all channels reference the *same* keylist
  (one curve, referenced 3×) — appears as 3 byte-identical curves.
- **Per-channel vector** (RGB colour): each channel references a *different*
  keylist with independent values.

> ⚠️ Grouping is a **heuristic** (adjacency + shared key-times). There is no
> confirmed explicit "component count" field in the property header, so the
> viewer labels grouped curves as inferred `vec2/3/4`, not ground truth.

---

## 5b. Property binding model — offset = parameter identity 🟢

The single most useful finding for reading effects. Animatable properties do
**not** encode *what* they drive in their type id — the type only encodes the
**data shape**. What a property drives is a **byte offset into the node's
parameter block** (`address_0x0`), stored in a field the early decoder
discarded.

| type | shape | binding-target field (now captured) |
|------|-------|-------------------------------------|
| `0x8F` | float curve | `target_offset` (Offset24) + `target_flags` + `f4` |
| `0x91` | float curve (variant) | `target_offset` (VarOffset 30/2) + `target_flags` |
| `0x8B` | scalar + bound float | `target_offset` + `target_flags` |
| `0x1E` | bound scalar | `target_offset` + `target_flags` |
| `0x8A` | two indirect floats | `value_offsets` (into properties table, *not* param block) |

Consequences, all verified against raw bytes across the 55 phoenix files:

- **The offset is the "module identity."** Curves of the *same type* `0x8F`
  differ only by `target_offset`. Consecutive offsets 4 bytes apart with matching
  `target_flags` are the per-channel floats of one vecN (e.g. `@64/@68/@72` = a
  vec3). This is a **stronger** vector-grouping signal than the §5 keytime
  heuristic and is what [vfxb_viewer.py](vfxb_viewer.py) now groups on.
- **A slot is scalar OR curve, interchangeably.** The *same* offset appears as a
  static `0x8B` scalar in one effect and an animated `0x8F` curve in another.
  Proven at `@48` of the spark node: bound in **all 37** files that use
  `ptn_hinoko_02s_mask.tex`, as a scalar (flags=2) in some and a curve
  (flags=128) in others. **⇒ "more curves / fewer scalars" between two effects is
  just an authoring choice (animate vs. hold constant), not a structural
  difference.** Same template, same struct.
- **`target_flags` = binding mode**, not noise: `2` = static-scalar bind,
  `64`/`128` = curve bind (exact meaning of 64-vs-128 unconfirmed 🟡).

### Cross-file offset alignment (spark node, 37 files) 🟢/🟡

Grouping nodes by shared texture and tabulating `offset → binding` yields a
stable parameter struct. High-confidence reads come from values that repeat
verbatim across many unrelated effects:

| offset | binding | value pattern | meaning | conf |
|--------|---------|---------------|---------|------|
| `@0`  | scalar, always `1.0` (25 files) | constant | enable/flag | 🟡 |
| `@48` | scalar↔curve (all 37 files) | 0 / curve | universal master knob | 🟡 |
| `@64/68/72` | vec3 curve, flags 64 | R∈[.18,1] G∈[.008,.24] B∈[0,.06] | **RGB colour** (fire-orange, identical across files) | 🟢 |
| `@76` | curve, flags 64 | [0,1] | **alpha over life** | 🟢 |
| `@400/404` | curve pair | [0,~3.8] | scale / size over life | 🟡 |
| `@420–@456` | curves | 0.001–30 | secondary knobs (velocity/emit) | 🔴 |

Built by **[vfxb_offset_align.py](vfxb_offset_align.py)** (see §10).

---

## 5c. The CPU→GPU boundary — offsets do NOT match shader buffers 🟢

Critical caveat when relating `.vfxb` offsets to the extracted GLSL. They live in
**two different address spaces** with a CPU repack in between:

- `.vfxb` `target_offset` = byte offset in the **authoring parameter struct**.
- render GLSL reads a **runtime GPU particle record** (`_14`, stride 15×`uvec4`
  = 240 B; plus a parallel stride-60-`uint` buffer `_9`).

The absolute numbers cannot align. The **semantic grouping** does, but only for
parameters that survive to the GPU as distinct fields:

| parameter | `.vfxb` | GPU record | endpoint-confirmable? |
|-----------|---------|-----------|-----------------------|
| colour RGB | `@64/68/72` (3 contiguous floats) | `uvec4[9].xyz` (`_174.xyz`) | **yes — both are a contiguous RGBA 4-tuple** 🟢 |
| alpha | `@76` | `uvec4[9].w` (`_174.w`) | **yes** 🟢 |
| position | (offset TBD) | `uvec4[0/1/2].w` (basis `.w` cols) | grouping matches 🟡 |
| **scale** | `@400/404` | **none** — folded into basis-vector *magnitude* of `uvec4[0/1/2]` | **no** — dissolves into the matrix, no distinct slot to match 🔴 |

So scale is a *derived* quantity: the CPU multiplies unit-orientation × scale
curve and stores the combined matrix rows; the render VS does `vertex · basis`
with **no separate scale multiply**. The 1 m authored quad → tiny spark shrink
happens at upload, invisible in both the render shader and any single GPU field.

**This is also why no compute shader integrates particle position** (see the
`_sim_scan` result: only `rain.tec` GPU-simulates; the phoenix stride-15 buffer
is never written by a compute shader). Transforms are evaluated **CPU-side** from
the `.vfxb` curves and uploaded. Confirming the CPU repack rule (which `.vfxb`
offset → which GPU byte) needs the exe or a frame capture; the static files can
prove endpoints but not the transform.

---

## 6. Textures — fully resolved 🟢

Per-node texture references work end-to-end:

```
node's 0x31 property
   → group offset (short at property +0x26)          # NOT +0x2A — see bug log
   → texture_groups[]   (8 signed-byte indices, -1 = empty)
   → texture_string_indices[]   (int64 table of string indices)
   → strings[]   → "vfx/asset/texture/.../*.tex"
```

Each `0x31` group holds up to 8 slot indices; each valid index maps through
`texture_string_indices` to a `.tex` path. This is the mechanism the viewer uses
to show a node's textures.

**⚠️ A `0x31` can carry NO texture group.** Some render passes sample the
*framebuffer* rather than a texture — a pure **distortion** pass (e.g. lost's
`0x80C24E06`, which runs `shader_0013_u1_u2.glsl` and reads no `.tex` at all).
Those props leave the group field unset, which reads as **offset 0** and would
silently alias `texture_groups[0]`, giving the node a bogus copy of whichever
node legitimately uses group 0.

The reliable tell is the **word at property `+0x28`**: it is non-zero for every
real group reference and zero only for the group-less ones. Verified across 120
files / 1089 `0x31` props:

| case | count | marker `@+0x28` set |
|------|-------|---------------------|
| `group_off != 0` | 894 | ✅ all |
| `group_off == 0`, real group-0 user | 179 | ✅ all |
| `group_off == 0`, **no group** | 16 | ❌ none |
| `group_off != 0` without marker | **0** | — (no counterexamples) |

Note the 179 legitimate group-0 users: testing `group_off == 0` alone would
wrongly strip their textures — the `+0x28` marker is what disambiguates.
The decoder emits `kind: "texture_slots"` + `has_texture_group: false` for the
group-less case (real ones are `kind: "texture_group"` + `group_offset`), and the
viewer paints those nodes with a distinct **`distortion`** role (violet) so an
untextured pass can never masquerade as a textured one. See §9 bug log.

### Editing `.tex` / `.mdl` paths 🟢

[vfxb_viewer.py](vfxb_viewer.py) exposes the file-global asset strings through
**Edit texture / model paths…**. Applying changes calls
`vfxb_encode.patch_asset_strings`, which rebuilds the complete NUL-terminated
string table in one pass so every texture's numeric string index remains stable.

The string blob's existing allocation (up to `blob_0x2.address`) is reused when
the edited table still fits. If a longer path needs more room, the allocation is
grown in 0x10-byte units, the following `blob_0x2` and item blobs are shifted as
opaque byte ranges, and their two absolute addresses in the pointer table are
updated. All item-blob sub-offsets stay valid because they are relative to the
item-blob base. The replacement must keep its original `.tex` or `.mdl`
extension.

---

## 7. Models (`.mdl`) — inferred for textured render nodes 🟡

There is no explicit model-index table, but the string-table serialization
order provides the missing renderer binding. Asset strings are authored in
**renderer blocks**:

```
model.mdl                 ← starts a renderer asset block
primary_texture.tex       ← first texture slot anchors the node to that block
additional_texture.tex
...
next_model.mdl            ← starts the next block
```

For a node's `0x31` texture group, resolve its first valid texture slot through
`texture_string_indices`, then walk backward in `strings[]` to the nearest
preceding `.mdl`. Later texture slots may reuse textures from other blocks, so
they must not participate in choosing the model.

Confirmed example (`pnx_wing_app_nml_01s.vfxb`):

| node | primary texture string | preceding model string | resolved model |
|------|------------------------|------------------------|----------------|
| `0xF1519D00` | `1` | `0` | `disk_basic_uv3_01s.mdl` |
| `0x80B98A31` | `5` | `4` | `quad.mdl` |

Across a read-only check of 300 extracted VFXB files, this resolved 2,311 of
2,329 textured nodes without ambiguity. The remaining 18 had no preceding
model entry and stay unresolved. `vfxb_decode.py` exposes the result as
`node.model`, `node.model_string_index`, and `node.model_binding`; the viewer
labels it **Model (inferred)**.

Why this remained hidden in the individual property payloads:

- No `model_string_indices` table (unlike textures).
- Only `quad.mdl` (the authored placeholder mesh, `vfx/tools_reference/…`) ever
  appears by string byte-offset — via `0x90` — and only in ~90 of 351 model
  references across all files. So `0x90` is **not** a general model pointer.
- ~~The single "constant" entry always resolves to `strings[0]`…~~ **Retracted.**
  This was a decode bug, not a data fact: constant names were being read from the
  global string blob (they belong to a constant-local region), so they *looked*
  like model strings. Constants are bind-points, not models (§8, §9). They say
  nothing about model binding either way.

**Hash binding was tested and not supported.** On the phoenix wing files,
FNV1a/FNV1/CRC32 of each `.mdl` path (full / lowercase / basename / backslash)
matched no node hash and appeared as no word in the item region.

**Complementary lead — ordinal binding (user-observed, partially checked 🟡):** render
leaves appear in the **same order** as the `.mdl` load manifest. A render leaf =
a leaf node (no children) carrying a `0x31` property. On `pnx_wing_lost`:

| # | render leaf | `referenced_models[i]` |
|---|-------------|------------------------|
| 0 | `0x4D0C01AA` | `cross_disk_ora_uv3_01s.mdl` |
| 1 | `0xF1519D00` | `disk_basic_uv3_01s.mdl` |
| 2 | `0xE6ED8115` | `gr_en_uv_01s.mdl` |
| 3 | `0x80C24E06` | `quad.mdl` ← shared |
| 4 | `0x80B98A31` | `quad.mdl` ← shared |
| — | *(attach mesh)* | `body.mdl` |

Consistent with a **positional** binding — invisible to both hash and offset scans.
Two caveats keep this 🟡, not 🟢: the mapping is **not strictly 1:1** (the
distortion pass and the spark both sit on the placeholder `quad.mdl`), and
`body.mdl` is unpaired (it is the character attach mesh, not a particle mesh).
Note leaf 3 is the group-less distortion pass (§6) — so lost has **5 render
leaves but only 4 textured** ones; count leaves by `0x31` presence, not by
resolved textures. Still to do: check whether leaf traversal order matches
manifest order across many files, and what breaks the tie when a model is shared.

Models that do not precede a primary texture remain file-level manifest entries.
For example, `body.mdl` in the Phoenix file follows the last renderer block and
is not assigned to either render node; it may instead describe a binder or
model-based emitter/spawn shape.

---

## 7b. Rosetta Stone — FF14 `.avfx` (same engine lineage) 🟢/🟡

FF14's `.avfx` format is **fully decoded and human-labelled** by the
[VFXEditor](https://github.com/0ceal0t/Dalamud-VFXEditor) plugin
(`VFXEditor/Formats/AvfxFormat/`). It is a **different serialization of the same
conceptual VFX engine**: FF14 uses reversed-4CC named blocks
(`ReadNested`: `4cc name + i32 size + payload`, 4-byte padded); FF16 uses numeric
type-ids + offset bindings (§5b). The *data model* matches, so FF14's names are a
concept dictionary for FF16's unlabelled offsets. **Use it to name, not to
parse** — the byte layouts differ.

### Confirmed correspondences

| concept | FF14 `.avfx` | FF16 `.vfxb` | conf |
|---------|--------------|--------------|------|
| node graph | `Scheduler → Timeline → Emitter → Particle` (+ Binder/Effector/Texture/Model) in `AvfxMain` | System → Emitter(container) → Renderer(leaf) node tree | 🟢 |
| spawn model = **rate × lifetime** | Emitter `CrC` Create Count, `CrI` Create Interval (both **curves**), `Life` | no per-node count; rate/life curves — matches (answers "why no count") | 🟢 |
| sim / render split | **Emitter** = spawn shape (`Cone/Sphere/Cylinder/Model`) + timing + initial velocity; **Particle** = per-life curves + all render state (DrawMode, textures TC1-4/TN/TR/TD/TP) | emitter/container = sim, leaf = render (§5c) | 🟢 |
| spawn shape mesh | `EmitterType.SphereModel/Model` | `gen_sph_01s.mdl`, spark container | 🟢 |
| render primitive types | `ParticleType.Quad/Model/Disc/Laser/Polyline/…` | quad.mdl / disk.mdl / mesh leaves | 🟢 |
| animation key | `Time:i16, Type:enum, Data:f32×3`; `KeyType {Spline=0, Linear=1, Step=2}` | key `type:i32, time, value(+tangents)` (§5) | 🟡 |
| **nested emitter→child containers** | `ItPr`/`ItEm` written **cumulatively**: i-th chunk holds items 0..i; reader takes the LAST as the full list | spark container has 2× `0x2B` groups pointing at the same geometry (our decoder flagged the 2nd as a "cycle") — **same cumulative-nesting quirk, not a real cycle** | 🟡 |

### What this resolves

- **"No particle count"** (§11) — confirmed by design: FF14 also has none; spawn is
  `CreateCount × CreateInterval` curves.
- **The "cycle" in our item tree** (§9-adjacent) — is almost certainly the FF14-style
  **cumulative container nesting**, where the same child is repeated across nested
  groups, not a genuine self-reference. Worth re-checking the `0x2B` walker with
  this in mind (take last group as authoritative).
- **Concept inventory for offset labelling** — FF14 exposes every parameter's name
  (e.g. `RvSx` Revised Scale X, `Pos/Rot/Scl`, Gravity, InjectionAngle). These are
  the candidate meanings to match FF16 offsets against by value-range/position.

### Node types to look for in FF16 (from FF14's 8 top-level lists)

FF14 `AvfxMain` dispatches 8 node types: **Scheduler** (`Schd`, timed driver) →
**Timeline** (`TmLn`) → **Emitter** (`Emit`) → **Particle** (`Ptcl`), plus
**Effector** (`Efct`, forces: point light, blur, camera quake), **Binder**
(`Bind`, attaches the effect to a bone/point), **Texture** (`Tex`), **Model**.
We have firm FF16 analogues only for Emitter/Particle(render)/Texture/Model. The
**Scheduler / Timeline / Binder / Effector** layers are candidates for the FF16
node hashes we have not yet identified — Binder in particular would explain how
an effect attaches to the summon's hand/wing (the `body.mdl` / bone reference).

### What does NOT transfer — real divergences

- **Reference mechanism differs.** FF14 references children by **integer index
  into typed sibling node-lists** (`TgtB`, `EmNo`, `TxNo`, resolved via
  `AvfxNodeSelect`). FF16 uses a **containment tree + offset bindings** (§5b) — no
  evidence yet of FF14-style index-into-typed-lists. This is the biggest
  structural difference; do not assume FF16 has per-type node arrays.
- **FF14 has no separate renderer object** — rendering state lives *inside* the
  Particle node. FF16 appears to split render into its own **leaf node** under an
  emitter/container. So the sim/render boundary is the same, but the *node
  granularity* differs (FF16 is finer).
- **Key record differs.** FF14 key = fixed **16-byte** record
  `Time:i16, Type:2B(Spline/Linear/Step), Data:f32×3` (Z = value, X/Y = spline
  tangents; RGB for colour). FF16 keys are variable (`type:i32,time,value+…`, §5).
  Same *idea* (inline interp tag), different bytes.
- Byte layout (reversed-4CC TLV vs type-id+offset) — cannot parse `.vfxb` with
  FF14 code.
- **FF16 added GPU simulation** (the `rain.tec` compute integrator); FF14 is
  CPU-era, so its model won't cover FF16's GPU particle path.
- Parameter names map by *concept*, not 1:1 offset — still requires the
  behavioural alignment from [vfxb_offset_align.py](vfxb_offset_align.py).

---

## 8. Other sections

| section | status |
|---------|--------|
| strings | 🟢 NUL-terminated `.mdl` / `.tex` paths, interleaved (no fixed index parity) |
| renderer models | 🟡 inferred from `.mdl` asset block preceding the node's primary texture string (§7) |
| constants | 🟢 **bind-point / transform-anchor references**, not shader math. Each entry = `type(u8) + name_len(u8) + name_off(u8) + f3 + f4 + f5 + f6`, name in a **constant-LOCAL** string region (`constants_address + count*0x10 + 0x10`, NOT the global blob), value at `constant_data_address + f6`. Names seen: `VFX_MONS_01`, `VFX_CharaCenter/Root`, `VFX_WeaponCenter/Root` (bone/point the effect attaches to — the **Binder** concept, §7b). `type 0` value is always float **1.0** (an "enabled" flag); `type 1` = vec4; other types (2/9/11/13) are scalars (13 is a small int index). See §9 bug log. |
| texture_indices | 🟢 int64 table, indexed by texture groups |
| vertex_sets | 🟢 named selection sets (short offset table + strings) |
| project_info | 🟡 holds an `item_count`; other fields unknown |
| shader blob | 🟡 `TEC` container with N embedded DXBC programs; **node→program mapping is not explicitly encoded** in the understood vfxb/TEC fields. The tools currently infer it by pairing render leaves and consecutive program groups in authored order; programs sharing a vertex shader are treated as variants of one particle renderer. Contains **only render (VS/PS) shaders** — no particle simulation (see §5c); the embedded compute (`u1=5`) is a lighting-gather pass, not a position integrator |
| `.vatb` files | ⛔ different format (no `VFXB` magic) — out of scope |

---

## 9. Bug log (things that were wrong and got fixed)

- **Texture group offset** was read at `+0x2A` (garbage that always resolved to
  the first texture); correct field is `+0x26`. Before the fix, every textured
  node falsely showed the same texture and some textures never appeared. 🟢 fixed
- **k2/k3 key values** were emitted as dict/list, which the plotter silently
  dropped (≈23% of keys invisible) and which produced `nan`/`1e34` junk from the
  wrong k2 layout. Now every key exposes a scalar `value` (+ `tangents`, and the
  16-byte `easing` for k2). 🟢 fixed
- **Curve `+inf` end-sentinel** collapsed plots to a vertical line (`inf` passed
  an `isinstance(float)` check); now handled by `math.isfinite` + clamping. 🟢 fixed
- **Item header 0x0C/0x10/0x14** were emitted only as an opaque `vec3`; they are
  actually `duration` / `lifetime_base` / `lifetime_range` (the editor timeline
  bar length + lifetime inputs). Now decoded as named, patchable fields; `vec3`
  is kept as an alias for backward compat. 🟢 fixed
- **Spawn-edge (`0x2B`) interval/count "curves"** were first dumped as a flat
  16-float window, which spilled the shared f6/f7 block together and produced
  nonsense readings (e.g. `interval=[0.34,4.9,10,1]`). They are keyframe chains
  walked via `backptr`; now decoded as real `{time,value,tangent}` keys. 🟢 fixed
- **`_key_table` sort crash** on curves whose keyframe time is the JSON sentinel
  string `"NaN"`/`"Infinity"` (`str < float` TypeError); sentinels now sort last.
  🟢 fixed
- **Constant NAMES resolved against the wrong string region.** `parse_constants`
  used the global string blob, but `TConstantEntry.name_offset` indexes a
  constant-LOCAL region (`constants_address + count*0x10 + 0x10`, per `vfxb.bt`).
  Every name came out as a `.mdl` path with a dropped leading `v` (e.g.
  `fx/asset/.../disk_basic_uv3_01s.mdl`) — a pure decode artifact. The real names
  are bind-points (`VFX_MONS_01`, `VFX_CharaRoot`…). This artifact also produced
  the old §7/§11 "constants resolve to a model string" claim, which was **false**.
  🟢 fixed
- **Constant VALUES dumped as raw ints.** The `ConstantDataType` enum in `vfxb.bt`
  only names `kVector4=1` / `kFloat32=3`, so every other type id (0/2/9/11/13,
  which real files use heavily) hit an "unknown ⇒ read as int" fallback and
  printed float `1.0` as its bit pattern `1065353216` (`0x3F800000`). Surveyed
  250 files: type 1 is vec4, **every other scalar type holds a float**. Now decoded
  as float (`value`), with the raw int kept as `value_int` for index-like types
  (e.g. 13). This also made `type 0` constants editable in the viewer. 🟢 fixed
- **Group-less `0x31` props aliased another node's textures.** A `0x31` that
  carries no texture group (a framebuffer/distortion pass) leaves the group field
  unset; reading it as offset `0` resolved to `texture_groups[0]`, so the node
  reported a **verbatim copy of whichever node legitimately uses group 0** —
  user-spotted on lost's `0x80C24E06` (a pure distortion pass running
  `shader_0013_u1_u2.glsl` that samples no `.tex`). Now gated on the `+0x28`
  marker word (§6): group-less props decode as `kind: "texture_slots"` /
  `has_texture_group: false`, and the viewer gives them a distinct `distortion`
  role instead of painting them as textured. Note the naive fix (`group_off == 0`
  ⇒ no textures) would have been **wrong** — 179 of 1089 props legitimately use
  group 0. 🟢 fixed

---

## 10. Tools

- **[vfxb_decode.py](vfxb_decode.py)** — `.vfxb` → JSON manifest (header, blobs,
  strings, constants, texture groups, full recursive node tree with decoded
  curves, `tree_stats`). Optionally carves the TEC shader blob to GLSL.
  ```
  python vfxb_decode.py <input.vfxb> [output.json]
  python vfxb_decode.py <input.vfxb> --no-shaders   # skip slow shader export
  ```
- **[vfxb_viewer.py](vfxb_viewer.py)** — PySide6 GUI: interactive node graph
  (pan/zoom), rich per-node cards with inline curve sparklines, overlaid
  RGB/XYZ plots for detected vector groups, and a right-panel inspector with
  full labelled curve plots. Curve/scalar labels now show the binding
  `@offset` (see §5b), vectors are grouped by contiguous offset, and the
  file-global asset editor can replace `.tex` / `.mdl` string-table paths.
  Shader extraction/conversion stays off during normal loads; use **Convert
  shaders to GLSL…** to run it in the background, show the inferred ordered
  shader group on each particle card, and view VS/PS GLSL in the inspector.
  ```
  python vfxb_viewer.py [file.vfxb | directory]
  ```
- **[vfxb_encode.py](vfxb_encode.py)** — bounded binary patch operations used
  by the viewer: fixed-width scalar edits plus asset string-blob rebuild and
  later-blob pointer fixups.
- **[vfxb_offset_align.py](vfxb_offset_align.py)** — cross-file parameter
  discovery. Decodes a folder, groups render nodes by shared texture (same
  texture ⇒ same template ⇒ same param struct), and tabulates
  `offset → {scalar|curve, value ranges}` across all files. Offsets that are
  always a vec3 curve → colour/scale; offsets that flip scalar↔curve → tunable
  knobs.
  ```
  python vfxb_offset_align.py <folder> [--tex substr] [--decode] [--json out]
  ```

---

## 11. Open questions / next steps

- **Semantic mapping (step 2):** *in progress* — the §5b binding-offset model +
  [vfxb_offset_align.py](vfxb_offset_align.py) turn this into a tractable
  offset→meaning table per node template. Colour/alpha confirmed; extend the
  alignment to the fire-mesh textures (`anm_fire_010/011`) and other node kinds.
  Use the **FF14 parameter dictionary** (§7b) as the candidate-name pool:
  match FF16 offsets to FF14 concepts (Life, CreateCount, InjectionAngle,
  Gravity, Revised Pos/Rot/Scl, Color) by value-range and node role.
- **Identify Scheduler/Timeline/Binder/Effector nodes** (§7b): FF14 has these 4
  layers we have not mapped in FF16. Binder = effect→bone attachment — check
  which node hash carries the `body.mdl` / bone reference. **Lead:** the
  **constants** section is the Binder data — its entries are bind-point names
  (`VFX_MONS_01`, `VFX_CharaCenter/Root`, `VFX_WeaponCenter/Root`; §8). Open: how
  a constant/bind-point ties to a specific node and to the `body.mdl` attach mesh.
- **Node references:** confirm FF16 has *no* FF14-style index-into-typed-node-lists
  (we only see containment + offset bindings). If an index table exists, it is
  unmapped.
- **`target_flags` meaning:** confirmed `2`=scalar-bind vs `64`/`128`=curve-bind;
  distinguish what `64` vs `128` select (likely per-particle vs per-emitter
  source struct). 🟡
- **CPU→GPU repack rule (§5c):** find the exe uploader or capture a frame to map
  a `.vfxb` `target_offset` to its GPU particle-record byte. Would promote
  position/scale labels to 🟢.
- **Model binding:** the preceding-model heuristic resolves textured nodes, while
  hash binding tested negative. Verify whether render-leaf order matches `.mdl`
  manifest order across many files, especially for shared models (`quad.mdl`),
  group-less distortion passes, and unpaired entries such as `body.mdl` (likely
  a binder target or model-based emitter/spawn shape).
- **Time unit:** keyframe times run 0–30+; likely frames or seconds — unconfirmed.
- **Particle count:** no per-node count exists; emission is rate×lifetime. A
  global pool size (e.g. `1024` on a root config node) is the only capacity-like
  value — confirm it is the pool cap. 🟡
