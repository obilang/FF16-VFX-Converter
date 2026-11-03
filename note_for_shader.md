## Reusable `PackedInstanceData` Layout Notes

Use the following as context when analyzing other shaders that bind:

```text
hostlayout.class.StructuredBuffer<PackedInstanceData>
```

### Resource information

```hlsl
StructuredBuffer<PackedInstanceData> InstanceData
    : register(t0, space0);
```

Captured resource:

```text
Binding:       VS SRV t0, space0
Resource:      Resource_2056
Buffer size:   2,457,600 bytes
Record count:  10,240
Record stride: 240 bytes (0xF0)
Dword count:   60 per record
```

A useful raw representation is:

```hlsl
struct PackedInstanceData
{
    uint raw[60]; // 60 × 4 = 240 bytes
};
```

Read float fields using:

```hlsl
float ReadFloat(uint recordIndex, uint fieldIndex)
{
    uint byteAddress =
        recordIndex * 0xF0 +
        fieldIndex * 4;

    return asfloat(LoadUint(byteAddress));
}
```

---

## Instance-to-record lookup

The draw call supplies `SV_InstanceID`, but it does not directly select a `PackedInstanceData` record.

The shader first uses:

```hlsl
cbuffer cIndicesList : register(b2, space0)
{
    uint4 packedIndices[32];
};
```

Each `uint4` contains eight packed `uint16` record indices:

```hlsl
uint GetRecordIndex(uint instanceID, uint baseInstance)
{
    uint localInstance = instanceID - baseInstance;

    uint4 packed =
        packedIndices[localInstance >> 3];

    uint word =
        packed[(localInstance >> 1) & 3];

    uint shift =
        (localInstance & 1) * 16;

    return (word >> shift) & 0xFFFF;
}
```

Equivalent mapping inside each `uint4`:

| Local instance | Packed component | Bits |
|---:|---|---|
| 0 | `.x` | `0–15` |
| 1 | `.x` | `16–31` |
| 2 | `.y` | `0–15` |
| 3 | `.y` | `16–31` |
| 4 | `.z` | `0–15` |
| 5 | `.z` | `16–31` |
| 6 | `.w` | `0–15` |
| 7 | `.w` | `16–31` |

For the analyzed draw:

```text
InstanceCount         = 1
StartInstanceLocation = 0
SV_InstanceID         = 0
localInstance         = 0

cIndicesList[0].x:
signed   = -2147483496
unsigned = 0x80000098

recordIndex = 0x80000098 & 0xFFFF
            = 0x0098
            = 152
```

---

## Address conversion

For record `R` and field/dword `F`:

```text
record base   = R × 240
field offset  = F × 4
byte address  = R × 240 + F × 4
```

Hexadecimal form:

```text
byte address = R × 0xF0 + F × 4
```

For record `152`:

```text
record base = 152 × 0xF0
            = 0x8E80
```

Example for field `44`:

```text
field offset = 44 × 4 = 0xB0
address      = 0x8E80 + 0xB0
             = 0x8F30
```

Read four bytes at that address as a little-endian `uint32`, then reinterpret the bits as an IEEE-754 float.

---

## Known/inferred record layout

```hlsl
struct PackedInstanceData
{
    // 0x00
    float4 transformRow0; // fields 0–3

    // 0x10
    float4 transformRow1; // fields 4–7

    // 0x20
    float4 transformRow2; // fields 8–11

    // Fields 12–35: unknown
    uint unknown12To35[24];

    // Fields 36–39: partially known
    uint unknown36;
    uint unknown37;
    uint unknown38;
    float alpha;          // field 39, inferred from COLOR.a multiplication

    // Fields 40–42: unknown
    uint unknown40To42[3];

    // Field 43
    uint packedTypeFlags; // upper 4 bits select cPerView entry

    // UV fields
    float uv0YOffset;     // field 44
    float uv1YOffset;     // field 45
    float uv1YScale;      // field 46
    float uv1XScale;      // field 47
    float uv2YOffset;     // field 48
    float uv2XOffset;     // field 49

    // Fields 50–59: unknown
    uint unknown50To59[10];
};
```

Byte-offset table:

| Field(s) | Byte offset | Size | Inferred meaning |
|---:|---:|---:|---|
| `0–3` | `0x00` | 16 B | Transform row 0 |
| `4–7` | `0x10` | 16 B | Transform row 1 |
| `8–11` | `0x20` | 16 B | Transform row 2 |
| `12–35` | `0x30` | 96 B | Unknown |
| `36–38` | `0x90` | 12 B | Unknown |
| `39` | `0x9C` | 4 B | Alpha, inferred |
| `40–42` | `0xA0` | 12 B | Unknown |
| `43` | `0xAC` | 4 B | Packed type/flags |
| `44` | `0xB0` | 4 B | UV0 Y offset |
| `45` | `0xB4` | 4 B | UV1 Y offset |
| `46` | `0xB8` | 4 B | UV1 Y scale |
| `47` | `0xBC` | 4 B | UV1 X scale |
| `48` | `0xC0` | 4 B | UV2 Y offset |
| `49` | `0xC4` | 4 B | UV2 X offset |
| `50–59` | `0xC8` | 40 B | Unknown |
| End | `0xF0` | — | Next record |

---

## UV formulas

Let:

```hlsl
float2 centeredUV = inputUV - 0.5;
```

### UV0

```hlsl
uv0.x = centeredUV.x + 0.5;
uv0.y = centeredUV.y * 0.6569
      + 0.5
      + instance.uv0YOffset;
```

Equivalent:

```hlsl
uv0.x = inputUV.x;
uv0.y = (inputUV.y - 0.5) * 0.6569
      + 0.5
      + instance.uv0YOffset;
```

### UV1

```hlsl
uv1.x = centeredUV.x * instance.uv1XScale
      + 0.3201;

uv1.y = centeredUV.y * instance.uv1YScale
      + 0.5
      + instance.uv1YOffset;
```

### UV2

```hlsl
uv2.x = centeredUV.x * 0.3984
      + 0.5
      + instance.uv2XOffset;

uv2.y = centeredUV.y * 0.4725
      + 0.5
      + instance.uv2YOffset;
```

Some decompiled expressions contain:

```hlsl
mad(-0.0, value, result)
```

The `-0.0 * value` term has no meaningful effect and can be removed.

---

## Decoded UV fields for record 152

Record base:

```text
0x8E80
```

| Field | Address | Little-endian bytes | Approx. float | Meaning |
|---:|---:|---|---:|---|
| 44 | `0x8F30` | `41 AA 90 3F` | `1.1302` | UV0 Y offset |
| 45 | `0x8F34` | `41 48 B5 3E` | `0.3541` | UV1 Y offset |
| 46 | `0x8F38` | `C1 A8 64 3F` | `0.8932` | UV1 Y scale |
| 47 | `0x8F3C` | `00 00 80 3F` | `1.0` | UV1 X scale |
| 48 | `0x8F40` | `2E AD EE 3E` | `0.4662` | UV2 Y offset |
| 49 | `0x8F44` | `8B 06 8B 3E` | `0.2715` | UV2 X offset |

---

## Other associated shader resources

| Register | Binding name | Decompiled form | Purpose |
|---|---|---|---|
| `b0` | `hostlayout.cCamera` | `float4[138]` | Camera/view/projection data |
| `t0` | `StructuredBuffer<PackedInstanceData>` | Raw/typed buffer loads | Per-record instance data |
| `b1` | `hostlayout.cPerView` | `float4[24]` | Per-view/type visibility |
| `b2` | `cIndicesList` | `float4[32]` | Packed `uint16` record indices |

### Known camera indices in this shader

| `cCamera` index | Inferred role |
|---:|---|
| `0–2` | Three-row view transform |
| `3–6` | Four-row view-projection transform |
| `10` | Custom depth-adjustment row |
| `37.xyz` | Camera position |

### Known `cPerView` behavior

```hlsl
uint typeIndex = instance.packedTypeFlags >> 28;
float visibility = cPerView[typeIndex].x;
```

The upper four bits of field `43` select a `cPerView` entry. Its `.x` value participates in visibility/alpha and can disable an instance.

---

## Confidence notes

Confirmed from shader arithmetic and capture:

- Record index lookup algorithm.
- Record index `152` for the analyzed draw.
- 60 dwords / 240-byte record interpretation.
- Numeric addresses and decoded values.
- Transform accesses at fields `0–11`.
- Fields `44–49` usage in UV calculations.
- Field `43` upper-nibble usage.

Inferred names, not confirmed original names:

- `alpha`
- `packedTypeFlags`
- `uv0YOffset`
- `uv1YOffset`
- `uv1YScale`
- `uv1XScale`
- `uv2YOffset`
- `uv2XOffset`

For future work, preserve both field numbers and inferred names, for example:

```hlsl
float uv0YOffset; // dword 44, byte offset 0xB0, inferred name
```