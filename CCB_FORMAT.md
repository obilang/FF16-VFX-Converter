# CCB collision geometry investigation

The record layout comes from Nenkai's `FF16_ccb_CharaCollisionBinary.bt`.
The shape meanings below are **inferences**, not names supplied by that
template or verified by tracing the game executable. Keep raw fields when
using these interpretations. The decoder exports them separately as `shape`.

## Evidence from c1001

The supplied version-1 `chara/c1001/collision/characollision.ccb` contains
1,091 records. Each entry points to a `Data0x20` payload:

| Payload type | Count | Nonzero payload floats after Type | Candidate geometry |
| --- | ---: | --- | --- |
| 2 | 825 | +0x14 only, positive (0.17–47) | Sphere, radius at +0x14 |
| 3 | 266 | +0x14 and +0x18, both positive | Capsule, radius and half-segment length |

All other payload words are zero in this sample. The corresponding entry
`UnkType` values are 3 and 2 respectively; this inverse mapping has not been
assigned a meaning. The `CollisionId` in `Data0x54` is a separate discriminator
and is not used to select sphere versus capsule.

Type-2 `Receive` entries attached to `j_head`, `j_spine_02` and `n_root` have
candidate radii 0.2, 0.4 and 0.5. Numerous type-3 sword entries attached to
`n_weapon_r` have dimensions 0.45 and 0.55, with Y position 0.55. These are
consistent with sphere hurt volumes and elongated weapon volumes, respectively.

Entry ID 5 has dimensions 0.5 and 0.4 and Y position 0.9. If +0x18 is the
half-distance between capsule cap centers, the resulting capsule spans
Y=0 to Y=1.8. This supports that interpretation, but does not prove it.
Full segment length, half-height including caps, and cylinder interpretations
have not been ruled out by engine code. The preview explicitly assumes the
capsule interpretation.

## Candidate field mapping

Offsets below are relative to the indicated structure, not the file.

| Structure | Offset | Storage | Inferred meaning |
| --- | --- | --- | --- |
| Data0x20 | +0x00 | int32 | 2: sphere candidate; 3: capsule candidate |
| Data0x20 | +0x14 | float32 | Radius |
| Data0x20, type 3 | +0x18 | float32 | Half-distance between cap centers |
| Entry | +0x2C, +0x30, +0x34 | 3 × float32 | Position XYZ |
| Entry | +0x38, +0x3C, +0x40 | 3 × float32 | Euler rotation components in radians |
| Entry | +0x24 | relative string offset | Joint name (known from the template) |

The supplied template declares +0x3C and +0x40 as integers. Reinterpreting
them as floats gives coherent angles: for example, Z=1.57079637 (90 degrees)
on `NearCameraFadeCollision`, and combinations of X=90/110 degrees with
Y=±30/±60 degrees on other entries. Original integer values are retained.

The decoder applies these interpretations only for version 1, matching entry
and payload types 2/3, zero-filled remaining payload words, finite transforms,
a positive radius and a nonnegative half-segment length. Other records retain
their raw data and an explicit reason why shape inference is unavailable.
Passing these checks establishes consistency with the sample, not proof of
the geometry semantics.

## Preview limits and remaining verification

The viewer shows a schematic side and end projection, centered at the origin.
For a candidate capsule it assumes a Y axis and computes total height as
`2 * (radius + half_segment_length)`. It does not apply the candidate position,
rotation, or bone transform. Units, coordinate handedness, Euler order, the
capsule's native axis, and the treatment of empty joint names are unconfirmed.

An accurate posed overlay requires both confirmation of these conventions and
the referenced skeleton's transforms (including the current animation pose).
The next decisive check is the game's code that consumes `Data0x20` types 2/3,
or controlled in-game comparison of changed dimensions. Additional character
samples can test consistency but cannot alone establish every convention.
