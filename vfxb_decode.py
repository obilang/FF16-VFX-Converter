"""
FF16 .vfxb (Visual Effect Binary) decoder.

Ports the 010 Editor template in VFX/vfxb.bt to Python and emits a JSON
manifest describing the effect graph.

What it extracts:
  - Header (data-section sizes, element counts)
  - Blob pointer table (shader / string / item blobs + sub-section offsets)
  - Shader blob ("TEC" container) - kept as a raw byte range reference
  - Strings (referenced .mdl / .tex asset paths)
  - Inferred textured-renderer model bindings from string asset-block ordering
  - Constants (named Vector4 / Float32 shader parameters)
  - Texture string indices + texture groups
  - Vertex set names
  - Project info (item count)
  - Full recursive Item tree: each Item is a graph node with a 32-bit type
    hash, a child count, timeline/lifetime fields (duration + lifetime_base +
    lifetime_range, from the item header at 0x0C/0x10/0x14), and a list of typed
    ItemProperty entries. Nested kItem (0x2B) groups are followed to any depth.
    Decoded property payloads:
      * keylist (0x8F/0x91) - animation curves (time/value keyframes)
      * scalar  (0x1E)      - single indirect float
      * scalar2 (0x8A/0x8B) - float pairs
      * texture_group (0x31)- resolved texture path set
      * generic - raw bytes + best-effort float/int interpretation
  - tree_stats: node count, max depth, total keyframes, inferred model-binding
    count, and histograms of property types and node hashes.

Non-finite floats (the +inf keyframe sentinel, or byte patterns that are not
really floats) are emitted as the strings "NaN"/"Infinity" so the JSON stays
strictly valid.

Usage:
    python vfxb_decode.py <input.vfxb> [output.json]
    python vfxb_decode.py <input.vfxb>        # writes <stem>.json next to input

By default the embedded TEC shader blob is also carved out, its DXBC shader(s)
extracted (via extract_shaders.py) and converted to GLSL (via convert_glsl.py)
into <output_dir>/<stem>_shaders/. The resulting GLSL path is recorded in the
JSON under shader_blob.glsl (plus a per-variation list in shader_blob.shaders).
Flags:
    --no-shaders   skip shader carving/extraction entirely
    --no-glsl      extract the .tec/.dxbc but skip the GLSL conversion step
    --keep-spv     keep intermediate .spv files
"""

import contextlib
import io
import json
import math
import struct
import sys
from pathlib import Path


MAGIC = b"VFXB"

# Header layout ---------------------------------------------------------------
# 0x00 char[4]  magic
# 0x04 byte     field_0x1
# 0x05 byte     field_0x2
# 0x06 byte     field_0x3
# 0x07 byte     padding
# 0x08 int      property_data_size
# 0x0C int      item_data_size
# 0x10 int      total_data_size
# 0x14 int      field_0x7
# 0x18 int      vertex_set_count
# 0x1C byte     texture_count
# 0x1D byte     constant_count      (field_0xA)
# 0x1E byte     field_0xB
# 0x1F byte     struct_0x2_count    (field_0xC)
# 0x20 byte     string_count
# ... more bytes ...
# 0x30          ItemEntry root_items[15]   (each 0x10)
# 0x120         byte unknown_data[0x98]
# 0x1B8         blob pointer table

HEADER_ROOT_ITEMS = 0x30
ROOT_ITEM_COUNT = 15
ROOT_ITEM_STRIDE = 0x10
BLOB_PTR_BASE = 0x1B8


class Reader:
    """Little-endian cursor over an in-memory buffer."""

    def __init__(self, data):
        self.d = data
        self.p = 0

    def seek(self, p):
        self.p = p

    def tell(self):
        return self.p

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def i8(self):
        v = struct.unpack_from("<b", self.d, self.p)[0]
        self.p += 1
        return v

    def i16(self):
        v = struct.unpack_from("<h", self.d, self.p)[0]
        self.p += 2
        return v

    def u16(self):
        v = struct.unpack_from("<H", self.d, self.p)[0]
        self.p += 2
        return v

    def i32(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def i64(self):
        v = struct.unpack_from("<q", self.d, self.p)[0]
        self.p += 8
        return v

    def f32(self):
        v = struct.unpack_from("<f", self.d, self.p)[0]
        self.p += 4
        return v

    def peek_u16(self):
        return struct.unpack_from("<H", self.d, self.p)[0]

    # Offset24: 24-bit signed offset + 8-bit flags packed in one int.
    def offset24(self):
        raw = self.u32()
        off = raw & 0x00FFFFFF
        if off & 0x00800000:
            off -= 0x01000000
        flags = (raw >> 24) & 0xFF
        return off, flags

    # VarOffset(30, 2): 30-bit signed offset + 2-bit flags.
    def var_offset(self, offset_bits=30, flag_bits=2):
        raw = self.u32()
        mask = (1 << offset_bits) - 1
        off = raw & mask
        if off & (1 << (offset_bits - 1)):
            off -= 1 << offset_bits
        flags = (raw >> offset_bits) & ((1 << flag_bits) - 1)
        return off, flags


def cstr(data, off):
    end = data.index(b"\x00", off)
    return data[off:end].decode("utf-8", "replace")


def sanitize(obj):
    """Replace non-finite floats (NaN/Inf) with strings so output is valid JSON.

    These arise from the +inf keyframe end-sentinel and from raw byte patterns
    read as float in slots that are not actually floats.
    """
    if isinstance(obj, float):
        if math.isnan(obj):
            return "NaN"
        if math.isinf(obj):
            return "Infinity" if obj > 0 else "-Infinity"
        return obj
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj


# ---------------------------------------------------------------- header/blobs

def parse_header(r):
    r.seek(0)
    magic = r.d[:4]
    if magic != MAGIC:
        raise ValueError(f"not a VFXB file (magic={magic!r})")
    h = {
        "magic": magic.decode("ascii"),
        "field_0x1": r.d[0x04],
        "field_0x2": r.d[0x05],
        "field_0x3": r.d[0x06],
        "property_data_size": struct.unpack_from("<i", r.d, 0x08)[0],
        "item_data_size": struct.unpack_from("<i", r.d, 0x0C)[0],
        "total_data_size": struct.unpack_from("<i", r.d, 0x10)[0],
        "field_0x7": struct.unpack_from("<i", r.d, 0x14)[0],
        "vertex_set_count": struct.unpack_from("<i", r.d, 0x18)[0],
        "texture_count": r.d[0x1C],
        "constant_count": r.d[0x1D],
        "field_0xB": r.d[0x1E],
        "struct_0x2_count": r.d[0x1F],
        "string_count": r.d[0x20],
    }
    return h


def parse_blob_ptrs(r):
    r.seek(BLOB_PTR_BASE)
    shader_addr = r.u32()
    shader_size = r.u32()

    string_addr = r.u32()
    string_f1 = r.u32()
    string_f2 = r.u32()

    blob2_addr = r.u32()
    blob2_f1 = r.u32()

    item_addr = r.u32()
    offs = [r.i32() for _ in range(14)]
    names = [
        "properties", "items", "constants", "field_0x4", "constant_data",
        "field_0x6", "field_0x7", "vertex_set", "texture_indices",
        "project_info", "field_0xB", "field_0xC", "field_0xD",
        "texture_groups",
    ]
    item = {"address": item_addr}
    for name, off in zip(names, offs):
        item[name + "_offset"] = off
        item[name + "_address"] = item_addr + off

    return {
        "shader_blob": {"address": shader_addr, "size": shader_size},
        "string_blob": {"address": string_addr, "f1": string_f1, "f2": string_f2},
        "blob_0x2": {"address": blob2_addr, "f1": blob2_f1},
        "item_blob": item,
    }


def parse_root_items(r):
    out = []
    for i in range(ROOT_ITEM_COUNT):
        base = HEADER_ROOT_ITEMS + i * ROOT_ITEM_STRIDE
        offset = struct.unpack_from("<i", r.d, base)[0]
        out.append(offset)
    return out


# ---------------------------------------------------------------- sub-sections

def parse_string_entries(r, string_addr, count):
    """Return string-table entries with their index and absolute byte address."""
    entries = []
    p = string_addr
    for index in range(count):
        s = cstr(r.d, p)
        raw_len = len(s.encode("utf-8", "replace"))
        lower = s.lower()
        kind = ("texture" if lower.endswith(".tex") else
                "model" if lower.endswith(".mdl") else None)
        entries.append({
            "index": index,
            "value": s,
            "address": p,
            "byte_length": raw_len,
            "kind": kind,
        })
        p += raw_len + 1
    return entries


def parse_strings(r, string_addr, count):
    """Compatibility wrapper returning only the string values."""
    return [entry["value"] for entry in
            parse_string_entries(r, string_addr, count)]


def parse_constants(r, blobs, strings, count):
    """Named shader parameters (Vector4 / Float32)."""
    if not count:
        return []
    item = blobs["item_blob"]
    const_addr = item["constants_address"]
    const_data_addr = item["constant_data_address"]
    # Constant NAMES live in a constant-LOCAL string region, NOT the global string
    # blob (see TConstantEntry in vfxb.bt): after the count*0x10 entry records and
    # a 0x10-byte byte_field, `name_offset` (u8) indexes into that local region.
    # Using the global string blob here mis-resolved every name to a .mdl path
    # (off-by-one artifact); the real names are shader params like "VFX_MONS_01".
    string_addr = const_addr + count * 0x10 + 0x10

    entries = []
    for i in range(count):
        base = const_addr + i * 0x10
        r.seek(base)
        typ = r.u8()
        name_len = r.u8()
        name_off = r.u8()
        _f3 = r.u8()
        _f4 = r.i32()
        _f5 = r.i32()
        f6 = r.i32()

        name = cstr(r.d, string_addr + name_off)
        val_addr = const_data_addr + f6
        r.seek(val_addr)
        # The vfxb.bt ConstantDataType enum only names kVector4=1 / kFloat32=3, but
        # real files carry many more type ids (0, 2, 9, 11, 13…). Surveyed across
        # 250 files, every scalar type holds a plain FLOAT (type 0 is always 1.0;
        # 9/11 are 0.0; 2/13 hold small floats/ints), and type 1 is a vec4. The old
        # "unknown => raw int" fallback printed float 1.0 as its bit pattern
        # 1065353216 (0x3F800000). Treat type 1 as vec4 and everything else as a
        # single float, but also keep the raw int so type-13-style index fields
        # (whose float reading is a denormal) stay recoverable.
        if typ == 0x1:  # kVector4
            value = list(struct.unpack_from("<4f", r.d, r.tell()))
            val_fmt = "4f"
        else:  # kFloat32 (3) and all other scalar constant types observed
            value = struct.unpack_from("<f", r.d, r.tell())[0]
            val_fmt = "f"
        entry = {
            "type": typ,
            "name": name,
            "value": value,
            # absolute byte position + format of the value (for in-place patching)
            "value_addr": val_addr,
            "value_fmt": val_fmt,
        }
        # keep the raw int too: some types (e.g. 13) use the slot as a small index
        # rather than a float, so the float reading is a meaningless denormal.
        if val_fmt == "f":
            entry["value_int"] = struct.unpack_from("<i", r.d, val_addr)[0]
        entries.append(entry)
    return entries


def parse_texture_indices(r, blobs, count):
    addr = blobs["item_blob"]["texture_indices_address"]
    return [struct.unpack_from("<q", r.d, addr + i * 8)[0] for i in range(count)]


def parse_vertex_sets(r, blobs, count):
    if not count:
        return []
    base = blobs["item_blob"]["vertex_set_address"]
    r.seek(base)
    name_offsets = [r.i16() for _ in range(count)]
    table_end = base + count * 2
    names = []
    for off in name_offsets:
        names.append(cstr(r.d, table_end + off))
    return names


def parse_project_info(r, blobs):
    base = blobs["item_blob"]["project_info_address"]
    return {
        "field_0x0": struct.unpack_from("<i", r.d, base)[0],
        "field_0x1": struct.unpack_from("<i", r.d, base + 4)[0],
        "item_count": struct.unpack_from("<i", r.d, base + 8)[0],
    }


# ---------------------------------------------------------------- keys / props

KEY_TYPE_NAMES = {0x0: "k0", 0x1: "k1", 0x2: "k2", 0x3: "kVector3"}


def parse_keylist(r):
    """Animation curve: a list of typed time/value keys.

    The per-key `type` tag selects the interpolation and the payload size
    (verified by byte-alignment across all sample files). In every variant the
    FIRST float after `time` is the keyframe VALUE, so `key["value"]` is always
    a scalar and always plottable:

        k0 (0x0) : stepped/constant   value                       (+4 bytes)
        k1 (0x1) : linear             value                       (+4 bytes)
        k3 (0x3) : value + 2 tangents value, tangent_in/out       (+12 bytes)
        k2 (0x2) : value + 2 tangents + a 16-byte custom easing
                   ramp (0..255 byte LUT)                          (+28 bytes)
    """
    count = r.i32()
    keys = []
    for _ in range(count):
        ktype = r.i32()
        time_off = r.tell()
        time = r.f32()
        # *_offset fields are ABSOLUTE byte positions of each editable float, so
        # the encoder can patch them in place (see vfxb_encode.patch_scalar).
        key = {"type": KEY_TYPE_NAMES.get(ktype, ktype), "time": time,
               "time_offset": time_off}
        if ktype == 0x0 or ktype == 0x1:
            key["value_offset"] = r.tell()
            key["value"] = r.f32()
        elif ktype == 0x3:  # value + 2 tangent handles
            base = r.tell()
            value, t_in, t_out = struct.unpack_from("<3f", r.d, base)
            key["value"] = value
            key["value_offset"] = base
            key["tangents"] = [t_in, t_out]
            key["tangent_offsets"] = [base + 4, base + 8]
            r.seek(base + 12)
        elif ktype == 0x2:  # value + 2 tangents + 16-byte easing ramp
            base = r.tell()
            value, t_in, t_out = struct.unpack_from("<3f", r.d, base)
            key["value"] = value
            key["value_offset"] = base
            key["tangents"] = [t_in, t_out]
            key["tangent_offsets"] = [base + 4, base + 8]
            key["easing"] = list(r.d[base + 12:base + 28])
            r.seek(base + 28)
        else:
            key["value"] = None
        keys.append(key)
    return {"count": count, "keys": keys}


def parse_texture_group(r, tex_indices, strings):
    """8 byte indices into texture_string_indices -> resolved texture paths."""
    indices = [r.i8() for _ in range(8)]
    textures = []
    string_indices = []
    for idx in indices:
        if idx == -1:
            continue
        if 0 <= idx < len(tex_indices):
            si = tex_indices[idx]
            if 0 <= si < len(strings):
                string_indices.append(si)
                textures.append(strings[si])
    return {"indices": indices, "string_indices": string_indices,
            "textures": textures}


def _safe_f32(data, off):
    if 0 <= off and off + 4 <= len(data):
        return struct.unpack_from("<f", data, off)[0]
    return None


def parse_property(r, blobs, addr_0x0, tex_indices, strings):
    """Parse a single ItemProperty.

    Returns (prop_dict, next_position, child_offsets) where child_offsets is a
    list of item offsets to recurse into (only non-empty for 0x2B kItem groups).
    """
    start = r.tell()
    ptype = r.peek_u16()
    props_addr = blobs["item_blob"]["properties_address"]
    items_addr = blobs["item_blob"]["items_address"]

    prop = {"type": f"0x{ptype:02X}", "offset": start}
    child_offsets = []

    if ptype == 0x2B:  # kItem: sub-item group = a SPAWN EDGE (GenerateItemProperty)
        # A 0x2B property is the graph's "timeline"/spawn connection from a
        # container to what it spawns (a particle OR a sub-emitter). Its 0x20-byte
        # header (see vfxb.bt GenerateItemProperty) carries the spawn count and a
        # set of offset fields; the spawn "Menu" params (spawn mode / interval /
        # count / max-at-once, per the SE editor) live in the struct(s) reached
        # through f6/f7/f8/f9. Field meanings are only partly known, so we surface
        # ALL of them raw here (see docstring) to expose patterns across files.
        r.u16()               # type
        size = r.i16()
        count = r.i32()       # spawn NUMBER (burst size); e.g. 3 = spawn 3 at once
        f3_off, f3_flags = r.var_offset()
        packed_f4 = r.u16()   # field_0x4 : 6 (slot index) + field_0x4a : 10
        f4 = packed_f4 & 0x3F
        f4a = packed_f4 >> 6
        f5 = r.i16()
        f6_off, f6_flags = r.offset24()   # -> spawn-schedule struct when set
        f7_off, f7_flags = r.offset24()   # -> spawn-schedule struct when set
        f8 = r.i32()          # -> props[f8] -> OffsetT -> props -> float
        f9 = r.i32()          # -> props[f9] -> OffsetT -> props -> float

        prop["kind"] = "item_group"
        prop["child_count"] = count
        prop["spawn_count"] = count
        prop["slot"] = f4
        prop["field_0x4a"] = f4a
        prop["field_0x5"] = f5
        prop["f6_offset"] = f6_off
        prop["f6_flags"] = f6_flags
        prop["f7_offset"] = f7_off
        prop["f7_flags"] = f7_flags
        prop["f8"] = f8
        prop["f9"] = f9
        # absolute byte positions of the header fields, for in-place patching.
        # spawn_count is a plain i32; field_0x5 is a plain i16; field_0x4a is the
        # high 10 bits of the u16 at +0xC (low 6 bits = slot), so it needs a
        # bitfield-aware patch (see vfxb_encode.patch_bits).
        prop["spawn_count_addr"] = start + 4
        prop["packed_f4_addr"] = start + 0xC      # u16: slot(0:6) | f4a(6:16)
        prop["field_0x5_addr"] = start + 0xE      # i16

        # item_info[count] lives at parent's address_0x0 + field_0x3.offset;
        # each entry is 0x10 bytes: OffsetT child, float weight, Offset24, Offset24.
        info_base = addr_0x0 + f3_off
        info = []
        if 0 <= count < 1_000_000:
            for k in range(count):
                eo = info_base + k * 0x10
                if eo + 0x10 <= len(r.d):
                    coff = struct.unpack_from("<i", r.d, eo)[0]
                    weight = struct.unpack_from("<f", r.d, eo + 4)[0]
                    child_offsets.append(coff)
                    info.append({"child_offset": coff, "weight": weight})
                elif eo + 4 <= len(r.d):
                    child_offsets.append(struct.unpack_from("<i", r.d, eo)[0])
        prop["item_info"] = info

        # A spawn edge carries FOUR independent parameter-curve pointers:
        #   f6 = spawn interval,  f7 = spawn count,  f8, f9 = two more params.
        # Each points at a run of fixed 16-byte records:
        #   {i32 backptr, i32 tag, i32 subtype, f32 value}
        # A record is a key while (tag & 0xFF) == 1 (0x201/sub 28 or 0x101/sub 21;
        # the tag/sub distinction is an interpolation flag, meaning TBD). Any of
        # the four pointers is -1 when that param is left at its default (defaults
        # are not stored).
        #
        # CRITICAL: the four runs are packed CONTIGUOUSLY in the properties table,
        # so a run ends at the NEXT-HIGHER of the four offsets — NOT at the first
        # non-key word (walking past the boundary bleeds the following pointer's
        # record into this curve; that produced bogus multi-key readings like the
        # spark count "[10, 1]" and interval "[15, 1]" — the trailing 1.0 was
        # actually f8's own single-value record).
        curve_offs = sorted(o for o in (f6_off, f7_off, f8, f9) if o is not None
                            and o >= 0)

        def _curve(off):
            if off is None or off < 0:
                return None
            nxt = [o for o in curve_offs if o > off]
            bound = min(nxt) if nxt else None
            keys = []
            o = off
            while len(keys) < 64:
                if bound is not None and o >= bound:
                    break
                base = props_addr + o
                if o < 0 or base + 16 > len(r.d):
                    break
                tag = struct.unpack_from("<i", r.d, base + 4)[0]
                if (tag & 0xFF) != 1 or ((tag >> 8) & 0xFF) not in (1, 2):
                    break  # not a key record -> end of run
                sub = struct.unpack_from("<i", r.d, base + 8)[0]
                val = _safe_f32(r.d, base + 12)
                keys.append({"time": float(len(keys)),
                             "value": round(val, 5) if val is not None
                             and math.isfinite(val) else val,
                             # absolute byte position of this value (for patching)
                             "value_addr": base + 12,
                             "tag": tag & 0xFFFF, "sub": sub})
                o += 16
            if not keys:
                return None
            primary = keys[0]["value"]
            extra = [k["value"] for k in keys[1:]]
            return {"offset": off, "keys": keys, "primary": primary,
                    "primary_addr": keys[0]["value_addr"], "extra": extra}

        prop["interval_curve"] = _curve(f6_off if f6_off >= 0 else None)
        prop["count_curve"] = _curve(f7_off if f7_off >= 0 else None)
        prop["f8_curve"] = _curve(f8 if f8 >= 0 else None)
        prop["f9_curve"] = _curve(f9 if f9 >= 0 else None)
        # convenience scalars (first value of each run), for quick display
        prop["f8_value"] = prop["f8_curve"]["primary"] if prop["f8_curve"] \
            else None
        prop["f9_value"] = prop["f9_curve"]["primary"] if prop["f9_curve"] \
            else None

        r.seek(start + size)
        return prop, start + size, child_offsets

    if ptype in (0x91, 0x8F):  # keyed animation curve -> KeyList
        r.u16()
        size = r.i16()
        f2 = r.i32()
        # field_0x3 = the BINDING TARGET: byte offset into this item's parameter
        # block (addr_0x0) of the value this curve animates. Curves with the same
        # type differ only by this offset - it is what makes each one a distinct
        # "module" (e.g. offsets 40/44/48 = the x/y/z of one vec3 parameter).
        if ptype == 0x91:
            tgt_off, tgt_flags = r.var_offset()
        else:
            tgt_off, tgt_flags = r.offset24()
        f4 = r.i32()
        r.seek(props_addr + f2)
        f5 = r.i32()
        r.seek(props_addr + f5)
        prop["kind"] = "keylist"
        prop["target_offset"] = tgt_off
        prop["target_flags"] = tgt_flags
        prop["f4"] = f4
        prop["keys"] = parse_keylist(r)
        r.seek(start + size)
        return prop, start + size, child_offsets

    if ptype == 0x31:  # texture group reference (GenerateProperty_0x31)
        r.u16()
        size = r.i16()
        # per the .bt layout the group offset is the `short offset` field at
        # +0x26 (after 8 ints + one short), NOT +0x2A. The group table is
        # addressed by byte offset, each TextureGroup being 8 bytes.
        r.seek(start + 0x26)
        group_off = r.i16()
        # A 0x31 can carry NO texture group at all (e.g. a pure distortion pass
        # that samples the framebuffer, not a texture). Those props leave the
        # group field unset, which reads as offset 0 and would silently alias
        # group[0] — giving the node a copy of some other node's textures.
        # The tell is the word at +0x28: every real group reference has it
        # non-zero (verified across 120 files / 1089 props: 894 with group!=0 and
        # 179 legitimately using group 0 ALL have it set; the 16 that don't are
        # exactly the group-less ones, with zero counterexamples).
        has_group = (start + 0x2C <= len(r.d)
                     and struct.unpack_from("<I", r.d, start + 0x28)[0] != 0)
        if not has_group:
            prop["kind"] = "texture_slots"
            prop["has_texture_group"] = False
            prop["group_offset"] = None
            r.seek(start + size)
            return prop, start + size, child_offsets
        r.seek(blobs["item_blob"]["texture_groups_address"] + group_off)
        prop["kind"] = "texture_group"
        prop["has_texture_group"] = True
        prop["group_offset"] = group_off
        prop["group"] = parse_texture_group(r, tex_indices, strings)
        r.seek(start + size)
        return prop, start + size, child_offsets

    if ptype == 0x8A:  # two indirect floats (GenerateProperty_0x8A)
        r.u16()
        size = r.i16()
        f2 = r.i32()
        f3 = r.i32()
        # both f2 and f3 index the properties table here (not the param block);
        # record them so the binding is visible.
        prop["kind"] = "scalar2"
        prop["value_offsets"] = [f2, f3]
        # absolute byte positions of the two floats (for in-place patching)
        prop["value_addrs"] = [props_addr + f2, props_addr + f3]
        prop["values"] = [_safe_f32(r.d, props_addr + f2),
                          _safe_f32(r.d, props_addr + f3)]
        r.seek(start + size)
        return prop, start + size, child_offsets

    if ptype == 0x8B:  # float(props+f2) + float(addr0+f3) (GenerateProperty_0x8B)
        r.u16()
        size = r.i16()
        f2 = r.i32()
        # f3 = binding target offset into this item's parameter block.
        f3_off, f3_flags = r.var_offset()
        prop["kind"] = "scalar2"
        prop["target_offset"] = f3_off
        prop["target_flags"] = f3_flags
        # absolute byte positions of the two floats (for in-place patching)
        prop["value_addrs"] = [props_addr + f2, addr_0x0 + f3_off]
        prop["values"] = [_safe_f32(r.d, props_addr + f2),
                          _safe_f32(r.d, addr_0x0 + f3_off)]
        r.seek(start + size)
        return prop, start + size, child_offsets

    if ptype == 0x1E:  # single indirect float (GenerateProperty_0x1E)
        r.u16()
        size = r.i16()
        # f2 = binding target offset into this item's parameter block.
        f2_off, f2_flags = r.var_offset()
        prop["kind"] = "scalar"
        prop["target_offset"] = f2_off
        prop["target_flags"] = f2_flags
        # absolute byte position of the float (for in-place patching)
        prop["value_addr"] = addr_0x0 + f2_off
        prop["value"] = _safe_f32(r.d, addr_0x0 + f2_off)
        r.seek(start + size)
        return prop, start + size, child_offsets

    # generic: type + size + payload; keep raw bytes and a best-effort numeric
    # interpretation so unknown property types are still inspectable.
    r.u16()
    size = r.i16()
    if size < 4:
        return prop, start + 4, child_offsets
    payload = bytes(r.d[start + 4:start + size])
    prop["kind"] = "generic"
    prop["size"] = size
    prop["raw"] = payload.hex()
    if len(payload) % 4 == 0 and payload:
        prop["as_floats"] = [round(v, 5) for v in
                             struct.unpack("<%df" % (len(payload) // 4), payload)]
        prop["as_ints"] = list(struct.unpack("<%di" % (len(payload) // 4),
                                             payload))
    r.seek(start + size)
    return prop, start + size, child_offsets


# ---------------------------------------------------------------- items

ITEM_HEADER_SIZE = 0x7C
MAX_TREE_DEPTH = 16


def parse_item(r, item_offset, blobs, tex_indices, strings, seen, depth=0):
    items_addr = blobs["item_blob"]["items_address"]
    props_addr = blobs["item_blob"]["properties_address"]
    abs_addr = items_addr + item_offset
    if abs_addr in seen:
        return {"offset": item_offset, "cycle": True}
    if depth > MAX_TREE_DEPTH or abs_addr < 0 or abs_addr + ITEM_HEADER_SIZE > len(r.d):
        return {"offset": item_offset, "out_of_range": True}
    seen.add(abs_addr)

    r.seek(abs_addr)
    field_0x0 = r.i32()
    field_0x1 = r.i32()
    packed = r.u32()
    property_size = packed & 0x00FFFFFF
    property_offset = (packed >> 24) & 0xFF
    # Timeline / lifetime fields, all floats in the 0x7C item header. CONFIRMED
    # against three known-answer files (each equals the length of a bar in the SE
    # editor timeline):
    #   0x0C = duration        -> the effective bar LENGTH ("lifetime" in editor)
    #   0x10 = lifetime_base   -> base/min lifetime  (== duration when no range)
    #   0x14 = lifetime_range  -> the "lifetime range" editor field (0 when unused;
    #                             the adjacent dropdown defaults to "plus")
    # In most nodes 0x10 == 0x0C and 0x14 == 0, but ~1 in 6 nodes carry a real
    # range (e.g. 0x10=21, 0x14=6, 0x0C=24) so these are distinct fields, not a
    # mirror. The bar START/offset is NOT stored in the item at all: two particles
    # at different start times have byte-identical records and the exact start
    # sequence is absent from the whole file, so start is derived at runtime.
    duration_off = abs_addr + 0x0C
    duration = r.f32()          # 0x0C
    lifetime_base = r.f32()     # 0x10
    lifetime_range = r.f32()    # 0x14
    # child_count sits at header offset 0x38 (see TItem field ordering)
    child_count = struct.unpack_from("<i", r.d, abs_addr + 0x38)[0]

    # hash sits at header offset 0x70 (uint hash, before field_0x1F/0x20)
    hash_val = struct.unpack_from("<I", r.d, abs_addr + 0x70)[0]

    addr_0x0 = props_addr + field_0x0

    item = {
        "offset": item_offset,
        "address": abs_addr,
        "hash": f"0x{hash_val:08X}",
        "child_count": child_count,
        # timeline / lifetime (see comment above); *_addr = absolute byte
        # position so the encoder can patch each value in place.
        "duration": duration,
        "duration_addr": duration_off,
        "lifetime_base": lifetime_base,
        "lifetime_base_addr": duration_off + 4,
        "lifetime_range": lifetime_range,
        "lifetime_range_addr": duration_off + 8,
        # kept for backwards compat with earlier JSON consumers
        "vec3": [duration, lifetime_base, lifetime_range],
        "properties": [],
        "children": [],
    }

    # property block follows the fixed 0x7C header
    r.seek(abs_addr + ITEM_HEADER_SIZE + property_offset)
    end_point = abs_addr + ITEM_HEADER_SIZE + property_offset + property_size
    guard = 0
    pending_children = []
    while r.tell() < end_point and guard < 4096:
        guard += 1
        pos = r.tell()
        try:
            prop, nxt, kids = parse_property(r, blobs, addr_0x0,
                                             tex_indices, strings)
        except (struct.error, IndexError, ValueError):
            break
        item["properties"].append(prop)
        pending_children.extend(kids)
        if nxt <= pos:
            break
        r.seek(nxt)

    # recurse into child items discovered in 0x2B groups
    for ckid in pending_children:
        item["children"].append(
            parse_item(r, ckid, blobs, tex_indices, strings, seen, depth + 1))

    return item


# ---------------------------------------------------------------- shader export

# TEC layout constants (see Output/shader/TEC.bt). ShaderHeader = 112 bytes.
_TEC_POS = 112
_TEC_STAGE = {0: "vertex", 1: "pixel"}


def _parse_tec_programs(blob):
    """Parse the ShaderProgram / indices / ShaderData tables of a TEC blob.

    A TEC groups its ShaderData blobs into ShaderPrograms (a ShaderProgram is
    one drawable pipeline = vertex + pixel stage). This grouping IS present in
    the file and is what we can report with confidence. Returns a dict with
    'programs' and 'shader_data', or None if the blob isn't a parseable TEC.
    """
    try:
        if blob[:3] != b"TEC":
            return None
        hdr = struct.unpack_from("<4s27I", blob, 0)
        (_magic, _flags, _csz, _padd, _unkOff1, _unkCnt1,
         sp_off, sp_cnt, _us_off, _us_cnt,
         idx_off, idx_cnt, sdt_off, s_cnt, *_rest) = hdr

        programs_raw = [struct.unpack_from("<II", blob, _TEC_POS + sp_off + i * 8)
                        for i in range(sp_cnt)]
        indices = [struct.unpack_from("<I", blob, _TEC_POS + idx_off + i * 4)[0]
                   for i in range(idx_cnt)]

        shader_data = []
        for i in range(s_cnt):
            e = _TEC_POS + sdt_off + i * 16
            _off, sz, di = struct.unpack_from("<III", blob, e)
            u1, u2 = struct.unpack_from("<BB", blob, e + 12)
            shader_data.append({"index": i, "size": sz, "define_idx": di,
                                "stage": u1, "u2": u2})

        programs = []
        for pi, (idx, cnt) in enumerate(programs_raw):
            di_list = [indices[idx + j] for j in range(cnt)
                       if 0 <= idx + j < len(indices)]
            programs.append({"program": pi, "shader_data_indices": di_list})

        return {"programs": programs, "shader_data": shader_data}
    except Exception:
        return None


def _annotate_model_bindings(items, string_entries):
    """Infer each textured render node's model from asset-block ordering.

    VFXB has no model-index table. Its global strings are instead authored in
    renderer blocks: a ``.mdl`` entry starts a block and is followed by that
    renderer's primary texture. Extra texture slots may reuse strings belonging
    to other blocks, so the first resolved texture slot is the block anchor.
    """
    model_entries = [entry for entry in string_entries
                     if entry.get("kind") == "model"]
    if not model_entries:
        return

    def visit(node):
        if "hash" not in node:
            return
        primary_index = None
        for prop in node.get("properties", []):
            if prop.get("kind") != "texture_group":
                continue
            indices = prop.get("group", {}).get("string_indices", [])
            if indices:
                primary_index = indices[0]
                break
        if primary_index is not None:
            preceding = [entry for entry in model_entries
                         if entry["index"] < primary_index]
            if preceding:
                model = preceding[-1]
                node["model"] = model["value"]
                node["model_string_index"] = model["index"]
                node["model_binding"] = {
                    "confidence": "inferred",
                    "method": "asset_block_before_primary_texture",
                    "primary_texture_string_index": primary_index,
                }
        for child in node.get("children", []):
            visit(child)

    for item in items:
        visit(item)


def _annotate_nodes(items, programs):
    """Pair ordered shader-program groups with ordered render/particle nodes.

    The VFXB does not contain a verified node -> TEC program index.  What is
    observable across files is that render leaves and shader groups appear in
    matching authored order.  Consecutive programs that use the same vertex
    ShaderData are treated as variants of one renderer (usually differing in
    their pixel shader), then those groups are paired with render leaves in
    depth-first item order.

    The annotation deliberately records this as an inference.  A count
    mismatch leaves nodes/groups unmatched instead of silently wrapping or
    assigning every program to every node.
    """
    if not programs:
        return

    def is_render(node):
        return (node.get("child_count") == 0
                and any(pr.get("type") == "0x31"
                        for pr in node.get("properties", [])))

    render_nodes = []

    def visit(node):
        if "hash" not in node:
            return
        if is_render(node):
            render_nodes.append(node)
        for c in node.get("children", []):
            visit(c)

    for it in items:
        visit(it)

    # Build contiguous renderer groups.  Keeping this contiguous is important:
    # if the same vertex shader is reused later by another renderer, it remains
    # a separate ordered group rather than being merged globally.
    # Compute/other programs are not particle render pipelines and must not
    # shift the ordered VS/PS pairing.
    render_programs = [p for p in programs
                       if p.get("vertex") or p.get("pixel")]
    program_groups = []
    previous_signature = object()
    for program in render_programs:
        vertex_signature = tuple(
            entry.get("shader_data_index") for entry in program.get("vertex", [])
        )
        # Programs without a vertex entry cannot be grouped safely.
        signature = vertex_signature or ("program", program.get("program"))
        if not program_groups or signature != previous_signature:
            program_groups.append([])
        program_groups[-1].append(program)
        previous_signature = signature

    node_count = len(render_nodes)
    group_count = len(program_groups)
    for order, node in enumerate(render_nodes):
        group = program_groups[order] if order < group_count else []
        program_indices = [p.get("program") for p in group]
        note = (
            f"Inferred by order: render particle {order + 1}/{node_count} is "
            f"paired with consecutive shader group {order + 1}/{group_count}. "
            "Programs sharing the same vertex shader are treated as variants. "
            "The VFXB/TEC data does not contain a verified node-program link."
        )
        if not group:
            note += " No shader group exists at this order."
        elif node_count != group_count:
            note += (f" Counts differ ({node_count} render particles, "
                     f"{group_count} shader groups).")
        node["shader"] = {
            "confidence": "inferred",
            "method": "render_and_shader_group_order",
            "particle_order": order,
            "shader_group_order": order if group else None,
            "program_indices": program_indices,
            "programs": group,
            "note": note,
        }


def export_shaders(vfxb_path, blobs, out_dir, *, do_glsl=True, keep_spv=False,
                   data=None):
    """Carve the embedded TEC blob, extract DXBC shader(s), convert to GLSL.

    Returns a dict describing what was produced (relative paths where possible),
    or a dict with an "error" key if anything went wrong. Never raises.
    """
    result = {}
    try:
        data = Path(vfxb_path).read_bytes() if data is None else bytes(data)
        addr = blobs["shader_blob"]["address"]
        size = blobs["shader_blob"]["size"]
        blob = data[addr:addr + size]
        if blob[:3] != b"TEC":
            return {"error": f"shader blob magic is {blob[:3]!r}, not TEC"}
        tec = _parse_tec_programs(blob)

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        stem = Path(vfxb_path).stem
        tec_path = out_dir / f"{stem}.tec"
        tec_path.write_bytes(blob)
        result["tec"] = str(tec_path)

        # --- DXBC extraction (reuse extract_shaders.parse_tec) ---
        shaders_dir = out_dir / f"{stem}_shaders"
        import extract_shaders as extractor
        # parse_tec prints a summary; capture it so it doesn't pollute our stdout.
        with contextlib.redirect_stdout(io.StringIO()):
            extractor.parse_tec(str(tec_path), str(shaders_dir))

        dxbc_files = sorted(shaders_dir.glob("*.dxbc"))
        if tec:
            expected_indices = {
                entry["index"] for entry in tec["shader_data"]
                if entry.get("size", 0) > 0
            }
            current_files = []
            for dxbc in dxbc_files:
                try:
                    data_index = int(dxbc.stem.split("_")[1])
                except (IndexError, ValueError):
                    continue
                if data_index in expected_indices:
                    current_files.append(dxbc)
            dxbc_files = current_files
        if not dxbc_files:
            result["error"] = "no .dxbc shaders extracted from TEC blob"
            return result

        # extract_shaders names files shader_<i:04d>_u<stage>_u<u2>.<ext>,
        # where <i> is the ShaderData index. Map that index -> file paths.
        by_data_idx = {}
        conversion_failures = 0
        for dxbc in dxbc_files:
            try:
                di = int(dxbc.stem.split("_")[1])
            except (IndexError, ValueError):
                continue
            by_data_idx[di] = {"dxbc": str(dxbc)}

        # --- GLSL conversion (reuse convert_glsl.convert_one) ---
        if do_glsl:
            import convert_glsl as converter
            missing = [t for t in converter.tool_paths()
                       if not Path(t).is_file()]
            if missing:
                result["error"] = "GLSL tools not found: " + ", ".join(missing)

        for dxbc in dxbc_files:
            try:
                di = int(dxbc.stem.split("_")[1])
            except (IndexError, ValueError):
                continue
            glsl = dxbc.with_suffix(".glsl")
            if do_glsl and "error" not in result:
                with contextlib.redirect_stdout(io.StringIO()):
                    ok = converter.convert_one(str(dxbc), keep_spv=keep_spv,
                                               force=True)
                if ok and glsl.is_file():
                    by_data_idx[di]["glsl"] = str(glsl)
                else:
                    by_data_idx[di]["glsl_error"] = "conversion failed"
                    conversion_failures += 1

        if conversion_failures and "error" not in result:
            result["error"] = (
                f"{conversion_failures} DXBC shader(s) failed GLSL conversion"
            )

        # --- Group ShaderData blobs into verified VS+PS programs ---
        result["shader_data"] = list(by_data_idx.values())
        if tec:
            stage_of = {sd["index"]: sd["stage"] for sd in tec["shader_data"]}
            programs = []
            for prog in tec["programs"]:
                p = {"program": prog["program"], "vertex": [], "pixel": []}
                for di in prog["shader_data_indices"]:
                    files = by_data_idx.get(di)
                    if not files:
                        continue
                    entry = dict(files, shader_data_index=di)
                    key = _TEC_STAGE.get(stage_of.get(di), "unknown")
                    p.setdefault(key, []).append(entry)
                programs.append(p)
            result["programs"] = programs
            result["program_count"] = len(programs)
    except Exception as exc:  # never let shader export break JSON output
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


# ---------------------------------------------------------------- top level

def decode(path, *, export_shaders_opt=True, do_glsl=True, keep_spv=False,
           out_dir=None, data=None):
    # `data` lets callers decode an in-memory (possibly edited) buffer while
    # still passing `path` for naming/shader export. When omitted, read `path`.
    if data is None:
        data = Path(path).read_bytes()
    else:
        data = bytes(data)
    r = Reader(data)

    header = parse_header(r)
    blobs = parse_blob_ptrs(r)
    root_offsets = parse_root_items(r)

    string_entries = parse_string_entries(
        r, blobs["string_blob"]["address"], header["string_count"])
    strings = [entry["value"] for entry in string_entries]
    tex_indices = parse_texture_indices(r, blobs, header["texture_count"])
    constants = parse_constants(r, blobs, strings, header["constant_count"])
    vertex_sets = parse_vertex_sets(r, blobs, header["vertex_set_count"])
    project_info = parse_project_info(r, blobs)

    seen = set()
    items = []
    for i, off in enumerate(root_offsets):
        if off == 1:  # sentinel = empty slot
            continue
        try:
            items.append(parse_item(r, off, blobs, tex_indices, strings, seen))
        except (struct.error, IndexError, ValueError) as e:
            items.append({"root_slot": i, "offset": off, "error": str(e)})

    _annotate_model_bindings(items, string_entries)

    # split referenced assets into models / textures for convenience
    models = [s for s in strings if s.lower().endswith(".mdl")]
    textures = [s for s in strings if s.lower().endswith(".tex")]

    stats = summarize_tree(items)

    shader_blob = {
        "address": blobs["shader_blob"]["address"],
        "size": blobs["shader_blob"]["size"],
        "magic": data[blobs["shader_blob"]["address"]:
                      blobs["shader_blob"]["address"] + 3].decode(
                      "ascii", "replace"),
    }

    if export_shaders_opt:
        if out_dir is None:
            out_dir = Path(path).parent
        shader_blob.update(export_shaders(
            path, blobs, out_dir, do_glsl=do_glsl, keep_spv=keep_spv,
            data=data))
        # Attach heuristic shader refs to render nodes (see _annotate_nodes).
        _annotate_nodes(items, shader_blob.get("programs"))

    return {
        "file": str(path),
        "size": len(data),
        "header": header,
        "blob_pointers": blobs,
        "root_item_offsets": root_offsets,
        "strings": strings,
        "string_entries": string_entries,
        "referenced_models": models,
        "referenced_textures": textures,
        "texture_string_indices": tex_indices,
        "constants": constants,
        "vertex_sets": vertex_sets,
        "project_info": project_info,
        "shader_blob": shader_blob,
        "tree_stats": stats,
        "items": items,
    }


def summarize_tree(items):
    """Aggregate node/property counts across the whole recursive tree."""
    node_count = 0
    max_depth = 0
    prop_type_counts = {}
    node_hash_counts = {}
    total_keys = 0
    model_binding_count = 0

    def visit(node, depth):
        nonlocal node_count, max_depth, total_keys, model_binding_count
        if "hash" not in node:
            return
        node_count += 1
        max_depth = max(max_depth, depth)
        node_hash_counts[node["hash"]] = node_hash_counts.get(node["hash"], 0) + 1
        if node.get("model"):
            model_binding_count += 1
        for p in node.get("properties", []):
            t = p["type"]
            prop_type_counts[t] = prop_type_counts.get(t, 0) + 1
            if p.get("kind") == "keylist":
                total_keys += p["keys"]["count"]
        for c in node.get("children", []):
            visit(c, depth + 1)

    for it in items:
        visit(it, 0)

    return {
        "node_count": node_count,
        "max_depth": max_depth,
        "total_keyframes": total_keys,
        "model_binding_count": model_binding_count,
        "property_type_counts": dict(sorted(prop_type_counts.items(),
                                            key=lambda kv: -kv[1])),
        "node_hash_counts": dict(sorted(node_hash_counts.items(),
                                        key=lambda kv: -kv[1])),
    }


def main(argv):
    flags = {a for a in argv[1:] if a.startswith("--")}
    positional = [a for a in argv[1:] if not a.startswith("--")]

    if not positional:
        print(__doc__)
        return 1

    export_shaders_opt = "--no-shaders" not in flags
    do_glsl = "--no-glsl" not in flags
    keep_spv = "--keep-spv" in flags

    inp = Path(positional[0])
    out = Path(positional[1]) if len(positional) > 1 else inp.with_suffix(".json")

    result = decode(inp, export_shaders_opt=export_shaders_opt,
                    do_glsl=do_glsl, keep_spv=keep_spv, out_dir=out.parent)
    out.write_text(json.dumps(sanitize(result), indent=2, allow_nan=False),
                   encoding="utf-8")

    h = result["header"]
    print(f"Decoded {inp.name} ({result['size']} bytes)")
    print(f"  strings         : {h['string_count']}")
    print(f"  models          : {len(result['referenced_models'])}")
    print(f"  textures        : {len(result['referenced_textures'])}")
    print(f"  constants       : {h['constant_count']}")
    print(f"  vertex sets     : {h['vertex_set_count']}")
    print(f"  project items   : {result['project_info']['item_count']}")
    st = result["tree_stats"]
    print(f"  graph nodes     : {st['node_count']} (max depth {st['max_depth']})")
    print(f"  model bindings  : {st['model_binding_count']} inferred")
    print(f"  keyframes total : {st['total_keyframes']}")
    print(f"  distinct props  : {len(st['property_type_counts'])} types")
    sb = result["shader_blob"]
    print(f"  shader blob     : {sb['magic']!r}"
          f" {sb['size']} bytes")
    if sb.get("shader_data"):
        print(f"  shader blobs    : {len(sb['shader_data'])} DXBC")
    if sb.get("program_count"):
        print(f"  programs (VS+PS): {sb['program_count']}")
    if sb.get("error"):
        print(f"  shader export   : SKIPPED ({sb['error']})")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
