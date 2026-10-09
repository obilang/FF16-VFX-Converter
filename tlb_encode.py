#!/usr/bin/env python3
"""In-place encoder / patcher for FF16 .tlb (FCTL) timeline files.

Design notes
------------
A .tlb file is NOT rebuilt from scratch here. The union payload blocks and
C-strings are scattered throughout the file (some sit *before* the timeline
base) and reference each other with relative offsets, so a full re-serialize
is fragile. Instead this module supports the two safe operations the viewer
needs:

  * patch_scalar()   — overwrite one fixed-width scalar (int/uint/bool/float)
                       in place. Same byte width, same location, no offset
                       fixups needed.

  * delete_element() — remove one timeline element using a "dead-slot" shift:
                       move the following 0x20-byte records down one slot,
                       bump their two element-relative offsets (name at +0x04,
                       data at +0x1C) by one slot size so they still resolve to
                       the same absolute targets, blank the trailing slot, and
                       decrement the element count. Verified to round-trip for
                       every element in the sample corpus.

String fields, asset paths, and the offset/count fields that describe array
layout are intentionally NOT editable — changing their width or value would
corrupt the cross-references.
"""

import struct
import shutil
from pathlib import Path

ELEM_SIZE = 0x20

# struct format code -> (python size, value kind, range)
_FMT = {
    "B": ("<B", 1, "int",   (0, 0xFF)),
    "H": ("<H", 2, "int",   (0, 0xFFFF)),
    "i": ("<i", 4, "int",   (-0x80000000, 0x7FFFFFFF)),
    "I": ("<I", 4, "int",   (0, 0xFFFFFFFF)),
    "f": ("<f", 4, "float", None),
    "d": ("<d", 8, "float", None),
}


def fmt_kind(fmt):
    """Return 'int', 'float', or 'bool' for a format code (bool = 'B' bool-ish)."""
    return _FMT[fmt][2]


def read_scalar(buf, offset, fmt):
    packfmt = _FMT[fmt][0]
    return struct.unpack_from(packfmt, buf, offset)[0]


def parse_value(fmt, text):
    """Parse a user string into a value valid for `fmt`. Raises ValueError."""
    packfmt, _size, kind, rng = _FMT[fmt]
    text = text.strip()
    if kind == "float":
        return float(text)
    # int
    val = int(text, 0) if text.lower().startswith(("0x", "-0x")) else int(text)
    lo, hi = rng
    if not (lo <= val <= hi):
        raise ValueError(f"value {val} out of range [{lo}, {hi}] for {fmt}")
    return val


def patch_scalar(buf, offset, fmt, value):
    """Write `value` at `offset` in the bytearray `buf` using format `fmt`."""
    packfmt = _FMT[fmt][0]
    struct.pack_into(packfmt, buf, offset, value)


# ─────────────────────────────────────────────── editable-field tables
#
# For each union_type, the scalar payload fields that are safe to edit,
# expressed as (key, offset_from_element_data_start, fmt).
# element-data start (ed) = union_start + 0x10.
# Fields that are string/asset offsets, array offset/count pairs, or hex
# blobs are deliberately omitted.

EDITABLE_UNION_FIELDS = {
    5:  [("bool_0x00", 0, "B"), ("bool_0x01", 1, "B"), ("bool_0x02", 2, "B"),
         ("chara_collision_shape_id", 4, "i")],
    8:  [("num_frames", 0, "i"), ("field_0x04", 4, "i")],
    9:  [("attack_param_id", 0, "i"), ("unk_type", 4, "i"),
         ("field_0x08", 8, "i"), ("eid_id", 12, "i")],
    10: [(f"bool_{i:02x}", i, "B") for i in range(9)] +
        [("field_0x0c", 12, "f"), ("field_0x10", 16, "i"), ("field_0x14", 20, "f")],
    11: [("motion_layer_id", 0, "i"), ("seconds_maybe", 4, "i"),
         ("field_0x08", 8, "i"), ("field_0x0c", 12, "i")],
    12: [("field_0x00", 0, "f"), ("field_0x04", 4, "f")] +
        [(f"bool_{i:02x}", 8 + i, "B") for i in range(8)] +
        [("vatb_entry_index_maybe", 16, "i")],
    27: [("bool_0x00", 0, "B")],
    30: [("field_0x08", 8, "i"), ("field_0x0c", 12, "B"), ("field_0x10", 16, "i"),
         ("field_0x14", 20, "i"), ("field_0x18", 24, "d"), ("field_0x20", 32, "d"),
         ("field_0x28", 40, "d"), ("field_0x30", 48, "i"), ("field_0x34", 52, "i"),
         ("field_0x38", 56, "i"), ("field_0x3c", 60, "f")],
    31: [("field_0x08", 8, "i"), ("field_0x0c", 12, "B"), ("field_0x10", 16, "i"),
         ("sab_entry_index_maybe", 20, "i"), ("field_0x18", 24, "d"),
         ("field_0x20", 32, "d"), ("field_0x28", 40, "d"), ("field_0x30", 48, "i"),
         ("play_vfx_trigger_set_id", 52, "i"), ("field_0x38", 56, "i"),
         ("field_0x3c", 60, "f"), ("bool_0x40", 64, "B")],
    33: [("field_0x00", 0, "i"), ("field_0x04", 4, "i")],
    37: [("packed_0x00", 0, "I"), ("field_0x04", 4, "f"), ("field_0x08", 8, "f"),
         ("field_0x0c", 12, "f"), ("field_0x14", 20, "f"), ("field_0x18", 24, "i"),
         ("field_0x1c", 28, "i"), ("linked_element_type", 44, "i"),
         ("field_0x3c", 60, "i")],
    45: [("se_index", 0, "i"), ("bool_0x04", 4, "B"), ("field_0x0c", 12, "i"),
         ("field_0x10", 16, "d"), ("field_0x18", 24, "d"), ("field_0x20", 32, "d")],
    47: [("battle_message_id", 0, "i")],
    49: [("mseq_input_id", 0, "i")],
    56: [("camera_fcurve_id", 0, "i"), ("field_0x08", 8, "i"), ("field_0x10", 16, "i"),
         ("field_0x18", 24, "i"), ("field_0x20", 32, "f"), ("field_0x24", 36, "i"),
         ("field_0x28", 40, "d"), ("field_0x30", 48, "f"), ("field_0x34", 52, "i")],
    60: [("field_0x00", 0, "i"), ("field_0x08", 8, "i"), ("field_0x10", 16, "i")],
    74: [("field_0x00", 0, "i")],
    1001: [("bool_0x08", 8, "B"), ("bool_0x09", 9, "B"), ("bool_0x0a", 10, "B"),
           ("float_0x0c", 12, "f"), ("bool_0x10", 16, "B"), ("bool_0x11", 17, "B"),
           ("bool_0x12", 18, "B")],
    1002: [("attack_param_id", 0, "i"), ("bool_0x08", 8, "B"),
           ("field_0x0c", 12, "i"), ("bool_0x18", 24, "B")],
    1004: [("field_0x00", 0, "i"), ("field_0x04", 4, "f"), ("field_0x08", 8, "f")],
    1007: [("flag", 0, "B")],
    1010: [("type", 0, "i"), ("target_type", 4, "i"), ("layout_instance_id", 8, "i"),
           ("field_0x0c", 12, "f"), ("field_0x10", 16, "i"), ("field_0x14", 20, "f"),
           ("field_0x18", 24, "f"), ("unk_type", 28, "i"), ("field_0x20", 32, "f")],
    1011: [("summon_magic_source_type", 0, "i"), ("custom_bool1", 4, "B"),
           ("custom_bool2", 5, "B"), ("custom_magic_id_skill1", 8, "i"),
           ("custom_magic_id_skill2", 12, "i")],
    1012: [("unused", 0, "i"), ("magic_id", 4, "i"),
           ("unk_bool_use_other_position", 8, "B"), ("has_target_maybe", 9, "B")],
    1014: [("flag", 0, "i")],
    1016: [("unk1", 8, "i"), ("unk2", 12, "i"), ("unk3", 16, "i")],
    1023: [("field_0x08", 8, "i"), ("play_vfx_trigger_set_id_maybe", 52, "i")],
    1047: [("summon_parts_pattern_id", 0, "i"), ("field_0x04", 4, "f"),
           ("field_0x08", 8, "f"), ("field_0x0c", 12, "B"), ("field_0x0d", 13, "B"),
           ("field_0x0e", 14, "B"), ("field_0x0f", 15, "B"), ("field_0x10", 16, "f")],
    1053: [("field_0x00", 0, "i"), ("battle_voice_category_id", 4, "i")],
    1056: [("frame_count_maybe", 0, "i"), ("frame_count2_maybe", 4, "i")],
    1059: [("field_0x04", 4, "f")],
    1064: [("unk_id", 0, "i")],
    1066: [("field_0x00", 0, "i")],
    1075: [("field_0x00", 0, "i")],
    1084: [("unk_type", 0, "i")],
    1086: [("height_fall", 0, "f"), ("only_if_target_hit", 4, "B"), ("unk", 5, "B")],
    1088: [("horizontal_type", 0, "i"), ("horizontal_force", 4, "f"),
           ("horizontal_rate", 8, "f"), ("unused", 12, "i"), ("vertical_type", 16, "i"),
           ("vertical_force", 20, "f"), ("vertical_rate", 24, "f"),
           ("field_0x1c", 28, "B")],
    1097: [("bool_0x04", 4, "B")],
    1099: [("field_0x00", 0, "i"), ("bool_0x04", 4, "B"), ("field_0x08", 8, "f"),
           ("unk_frames1", 12, "i"), ("unk_frames2", 16, "i"), ("field_0x14", 20, "f"),
           ("field_0x18", 24, "f"), ("field_0x1c", 28, "f")],
    1115: [("frames_maybe", 0, "i"), ("field_0x04", 4, "i"), ("field_0x08", 8, "f")],
    1117: [("unk_id", 0, "i")],
}
# 1035 shares PlayAnimationRange layout with 1001
EDITABLE_UNION_FIELDS[1035] = EDITABLE_UNION_FIELDS[1001]
# 57 shares PadVibration layout with 56
EDITABLE_UNION_FIELDS[57] = EDITABLE_UNION_FIELDS[56]
# 73 shares layout with 60
EDITABLE_UNION_FIELDS[73] = EDITABLE_UNION_FIELDS[60]

# VFX emit-params sub-struct (one spawn transform), 0x58 bytes each.
VFX_EMIT_PARAMS_SIZE = 0x58
EDITABLE_VFX_EMIT_FIELDS = [
    ("active", 0x00, "i"), ("unk_id_slot", 0x04, "i"),
    ("eid_id1", 0x08, "i"), ("eid_id2", 0x0C, "i"),
    ("offset_x", 0x10, "d"), ("offset_y", 0x18, "d"), ("offset_z", 0x20, "d"),
    ("revolution", 0x28, "d"), ("rotation", 0x30, "f"), ("scale", 0x34, "f"),
]

# VFX external-params override list, 0x1C bytes each.
VFX_EXTERNAL_PARAM_SIZE = 0x1C
EDITABLE_VFX_EXTERNAL_FIELDS = [
    ("param_id", 0x00, "i"), ("kind", 0x04, "i"),
    ("unk_0x08", 0x08, "i"), ("value", 0x0C, "f"),
]

# union_types whose payload begins with (vfx_params_off, count) at ed+0/ed+4.
_VFX_EMIT_TYPES = {1023, 1030, 1049}
# union_types that also carry an external-params list at ed+8/ed+12.
_VFX_EXTERNAL_TYPES = {1030, 1049}


# ─────────────────────────────────────────────── field-offset map

def _timeline_base(buf):
    return struct.unpack_from("<i", buf, 0x18)[0]


def build_field_map(buf, elem_index):
    """Return the editable-field layout for one element as a list of sections.

    Each section is {"title": str, "fields": [ {key, offset, fmt, value}, ... ]}.
    `offset` is the absolute byte offset into `buf`. The viewer builds one
    editor widget per field and calls patch_scalar() with these on Apply.
    """
    tb = _timeline_base(buf)
    elem_offset = struct.unpack_from("<i", buf, tb + 4)[0]
    elem_start = tb + elem_offset + elem_index * ELEM_SIZE

    data_off = struct.unpack_from("<i", buf, elem_start + 0x1C)[0]
    union_start = elem_start + data_off
    union_type = struct.unpack_from("<i", buf, union_start)[0]
    ed = union_start + 0x10

    sections = []

    def make_field(key, off, fmt):
        return {"key": key, "offset": off, "fmt": fmt,
                "value": read_scalar(buf, off, fmt)}

    # ── element header (absolute offsets from elem_start) ────────────────
    hdr = [
        make_field("field_0x00", elem_start + 0x00, "i"),
        make_field("layer_id",   elem_start + 0x08, "i"),
        make_field("frame_start", elem_start + 0x0C, "i"),
        make_field("num_frames",  elem_start + 0x10, "i"),
        make_field("field_0x14",  elem_start + 0x14, "i"),
        make_field("field_0x18[0]", elem_start + 0x18, "B"),
        make_field("field_0x18[1]", elem_start + 0x19, "B"),
        make_field("field_0x18[2]", elem_start + 0x1A, "B"),
        make_field("field_0x18[3]", elem_start + 0x1B, "B"),
    ]
    sections.append({"title": "Element header", "fields": hdr})

    # ── union header (union_field_0x04/08/0c; type stays read-only) ──────
    uhdr = [
        make_field("union_field_0x04", union_start + 0x04, "i"),
        make_field("union_field_0x08", union_start + 0x08, "i"),
        make_field("union_field_0x0c", union_start + 0x0C, "i"),
    ]
    sections.append({"title": "Union header", "fields": uhdr})

    # ── payload scalars ──────────────────────────────────────────────────
    spec = EDITABLE_UNION_FIELDS.get(union_type)
    if spec:
        pf = [make_field(key, ed + rel, fmt) for key, rel, fmt in spec]
        sections.append({"title": f"Payload  (type {union_type})", "fields": pf})

    # ── VFX emit params (per-instance spawn transforms) ──────────────────
    if union_type in _VFX_EMIT_TYPES:
        vfx_off = struct.unpack_from("<i", buf, ed + 0)[0]
        vfx_cnt = struct.unpack_from("<i", buf, ed + 4)[0]
        base = ed + vfx_off
        for i in range(max(vfx_cnt, 0)):
            entry = base + i * VFX_EMIT_PARAMS_SIZE
            if entry + VFX_EMIT_PARAMS_SIZE > len(buf):
                break
            f = [make_field(key, entry + rel, fmt)
                 for key, rel, fmt in EDITABLE_VFX_EMIT_FIELDS]
            sections.append({"title": f"vfx_emit_params[{i}]", "fields": f})

    # ── VFX external params (per-spawn overrides) ────────────────────────
    if union_type in _VFX_EXTERNAL_TYPES:
        ext_off = struct.unpack_from("<i", buf, ed + 8)[0]
        ext_cnt = struct.unpack_from("<i", buf, ed + 12)[0]
        base = ed + ext_off
        for i in range(max(ext_cnt, 0)):
            entry = base + i * VFX_EXTERNAL_PARAM_SIZE
            if entry + VFX_EXTERNAL_PARAM_SIZE > len(buf):
                break
            f = [make_field(key, entry + rel, fmt)
                 for key, rel, fmt in EDITABLE_VFX_EXTERNAL_FIELDS]
            sections.append({"title": f"vfx_external_params[{i}]", "fields": f})

    return sections


# ─────────────────────────────────────────────── element deletion

def delete_element(buf, index):
    """Return a new bytes object with timeline element `index` removed.

    Uses the dead-slot shift: records after `index` move down one 0x20 slot;
    their element-relative offsets (name at +0x04, data at +0x1C) are bumped
    by one slot so they still resolve to the same absolute bytes. The trailing
    slot is blanked and the element count decremented. Other tables (asset
    groups, targets) are element-index independent and left untouched.
    """
    buf = bytearray(buf)
    tb = _timeline_base(buf)
    elem_offset = struct.unpack_from("<i", buf, tb + 4)[0]
    elem_count = struct.unpack_from("<i", buf, tb + 8)[0]
    if not (0 <= index < elem_count):
        raise IndexError(f"element index {index} out of range (count {elem_count})")

    arr = tb + elem_offset
    for j in range(index, elem_count - 1):
        src = arr + (j + 1) * ELEM_SIZE
        rec = bytearray(buf[src:src + ELEM_SIZE])
        name_off = struct.unpack_from("<i", rec, 0x04)[0]
        data_off = struct.unpack_from("<i", rec, 0x1C)[0]
        # record moves down one slot (elem_start -= ELEM_SIZE), so relative
        # offsets must grow by ELEM_SIZE to point at the same absolute bytes.
        if name_off:
            struct.pack_into("<i", rec, 0x04, name_off + ELEM_SIZE)
        struct.pack_into("<i", rec, 0x1C, data_off + ELEM_SIZE)
        buf[arr + j * ELEM_SIZE:arr + (j + 1) * ELEM_SIZE] = rec

    # blank the now-unused trailing slot and drop the count
    last = arr + (elem_count - 1) * ELEM_SIZE
    buf[last:last + ELEM_SIZE] = b"\x00" * ELEM_SIZE
    struct.pack_into("<i", buf, tb + 8, elem_count - 1)
    return bytes(buf)


# ─────────────────────────────────────────────── element insertion
#
# Adding an element grows the fixed-stride element array by one 0x20 record.
# A mid-file insertion would shift every union payload, string, asset group and
# target that follows, and those blocks cross-reference each other with relative
# offsets in both directions — an exhaustive fixup that can't be guaranteed for
# raw-dumped unknown types.
#
# Instead we RELOCATE the whole element array to an aligned position at EOF and
# grow it there. Nothing in the file points *into* the array except the timeline
# header's elem_offset, so every existing union / string / asset group / target
# stays byte-for-byte in place; we only rewrite the two element-relative offsets
# in each moved record (so their absolute targets are unchanged) and update
# elem_offset + elem_count in the header.
#
# Only pure-scalar element types are offered for insertion (no string / asset-
# path / array-pointer payloads), so a synthesized default union is always valid.

# union_type -> {data_size, frames:(start,num), defaults:[(rel_off, fmt, value)]}
# rel_off is relative to element-data start (union_start + 0x10). data_size is
# the ElementData size (excludes the 0x10 union header); it is padded to 16.
ELEMENT_TEMPLATES = {
    5:    {"data_size": 0x08, "frames": (0, 30),
           "defaults": []},                                  # scalar bools + id
    10:   {"data_size": 0x18, "frames": (0, 30),
           "defaults": [(0x03, "B", 1)]},                    # BattleCondition
    12:   {"data_size": 0x18, "frames": (0, 30),
           "defaults": [(0x00, "f", 0.001), (0x04, "f", 1.0),
                        (0x0b, "B", 1)]},                     # BulletTimeRange
    27:   {"data_size": 0x04, "frames": (0, 30),
           "defaults": []},                                  # AttackMovement
    47:   {"data_size": 0x04, "frames": (0, 0),
           "defaults": []},                                  # BattleMessageRange
    1004: {"data_size": 0x0c, "frames": (0, 30), "defaults": []},
    1007: {"data_size": 0x04, "frames": (0, 30),
           "defaults": [(0x00, "B", 1)]},                    # FreezeAirAscendRate
    1010: {"data_size": 0x24, "frames": (0, 0),
           "defaults": []},                                  # TurnToTarget
    1014: {"data_size": 0x04, "frames": (0, 30),
           "defaults": [(0x00, "i", 1)]},                    # EnableBattleFlagRange
    1047: {"data_size": 0x14, "frames": (0, 30),
           "defaults": []},                                  # SummonPartsVisibleRange
    1053: {"data_size": 0x08, "frames": (0, 0),
           "defaults": []},                                  # BattleVoiceTrigger
    1084: {"data_size": 0x04, "frames": (0, 30),
           "defaults": []},                                  # EnableMagicBurstRange
    1086: {"data_size": 0x08, "frames": (0, 30),
           "defaults": []},                                  # AirborneFallRange
    1088: {"data_size": 0x20, "frames": (0, 0),
           "defaults": []},                                  # ApplyCharacterForce
    1117: {"data_size": 0x04, "frames": (0, 0),
           "defaults": []},                                  # ActionEventTrigger
}

# offered by the UI, in ascending type order
ADDABLE_TYPES = sorted(ELEMENT_TEMPLATES)


def type_label(union_type):
    """Human label 'BulletTimeRange (12)' for a union type, reusing the decoder
    name table (imported lazily to avoid a hard import cycle)."""
    import tlb_decode
    name = tlb_decode.ELEMENT_TYPE_NAMES.get(union_type, f"type_{union_type}")
    return f"{name} ({union_type})"


def _align16(buf):
    while len(buf) % 16:
        buf.append(0)


def add_element(buf, union_type):
    """Append a new element of `union_type`, returning (bytes, new_index).

    Uses the array-relocation strategy described above. `union_type` must be a
    key of ELEMENT_TEMPLATES.
    """
    tmpl = ELEMENT_TEMPLATES.get(union_type)
    if tmpl is None:
        raise ValueError(f"union type {union_type} is not addable")

    buf = bytearray(buf)
    tb = _timeline_base(buf)
    elem_offset = struct.unpack_from("<i", buf, tb + 4)[0]
    elem_count = struct.unpack_from("<i", buf, tb + 8)[0]
    old_base = tb + elem_offset

    _align16(buf)
    new_base = len(buf)
    delta = new_base - old_base   # every record moves forward by this much

    # ── rebuild the element array at the new base ────────────────────────
    arr = bytearray()
    for i in range(elem_count):
        rec = bytearray(buf[old_base + i * ELEM_SIZE:
                            old_base + (i + 1) * ELEM_SIZE])
        name_off = struct.unpack_from("<i", rec, 0x04)[0]
        data_off = struct.unpack_from("<i", rec, 0x1C)[0]
        # record moved forward by `delta`; relative offsets must shrink by the
        # same amount so their absolute targets are unchanged.
        if name_off:
            struct.pack_into("<i", rec, 0x04, name_off - delta)
        struct.pack_into("<i", rec, 0x1C, data_off - delta)
        arr += rec

    # ── new record (data_off patched after the union is placed) ──────────
    fs, nf = tmpl["frames"]
    new_rec = bytearray(ELEM_SIZE)
    struct.pack_into("<i", new_rec, 0x0C, fs)   # frame_start
    struct.pack_into("<i", new_rec, 0x10, nf)   # num_frames
    arr += new_rec

    buf += arr
    new_elem_start = new_base + elem_count * ELEM_SIZE

    # ── new union payload, aligned, appended after the array ─────────────
    _align16(buf)
    union_pos = len(buf)
    union = bytearray(0x10 + ((tmpl["data_size"] + 15) & ~15))
    struct.pack_into("<i", union, 0x00, union_type)   # UnionType; header 04/08/0c = 0
    ed = 0x10
    for rel, fmt, value in tmpl["defaults"]:
        struct.pack_into(_FMT[fmt][0], union, ed + rel, value)
    buf += union
    _align16(buf)

    # link the new record to its union
    struct.pack_into("<i", buf, new_elem_start + 0x1C, union_pos - new_elem_start)

    # ── clear the vacated old array region (cosmetic) & update header ────
    for k in range(old_base, old_base + elem_count * ELEM_SIZE):
        buf[k] = 0
    struct.pack_into("<i", buf, tb + 4, new_base - tb)      # elem_offset
    struct.pack_into("<i", buf, tb + 8, elem_count + 1)     # elem_count

    return bytes(buf), elem_count


# ─────────────────────────────────────────────── backup / restore

def origin_path(path):
    """foo.tlb -> foo_origin.tlb (sibling backup of the untouched original)."""
    p = Path(path)
    return p.with_name(f"{p.stem}_origin{p.suffix}")


def ensure_backup(path):
    """Copy `path` to its *_origin.tlb sibling once, if not already present.

    Returns the backup path. If the backup already exists it is left as-is so
    repeated saves never clobber the true original.
    """
    src = Path(path)
    bak = origin_path(src)
    if not bak.exists():
        shutil.copy2(src, bak)
    return bak


def has_backup(path):
    return origin_path(path).exists()


def restore_backup(path):
    """Copy the *_origin.tlb back over `path`. Returns True if a backup existed."""
    bak = origin_path(path)
    if not bak.exists():
        return False
    shutil.copy2(bak, Path(path))
    return True
