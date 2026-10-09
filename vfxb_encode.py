#!/usr/bin/env python3
"""In-place encoder / patcher for FF16 .vfxb (VFXB) effect-graph files.

Design notes
------------
A .vfxb file is NOT rebuilt from scratch here. The effect graph is a dense web
of blobs (shader TEC, string table, constant table, item tree, property table)
that reference each other with relative offsets, so a full re-serialize is
fragile. Instead — exactly like tlb_encode.py — this module supports two
bounded operations the viewer needs:

  * patch_scalar()  — overwrite one fixed-width scalar (float/int) in place.
                      Same byte width, same location, no offset fixups needed.
  * patch_asset_strings() — rebuild the central NUL-terminated string blob.
                      Its existing allocation is retained when possible; when
                      it must grow, the two later blobs are moved and their
                      absolute pointers are fixed up.

The vfxb decoder (vfxb_decode.py) records the ABSOLUTE byte position of every
editable value it reads:

  * keyframe curves  : key["value_offset"], key["time_offset"],
                       key["tangent_offsets"][0/1]
  * scalar  (0x1E)   : prop["value_addr"]
  * scalar2 (0x8A)   : prop["value_addrs"][0/1]   (both index the prop table)
  * scalar2 (0x8B)   : prop["value_addrs"][0/1]   (f2 in prop table, f3 in item)
  * constants        : const["value_addr"] + const["value_fmt"]

The viewer builds a QLineEdit per value from those offsets and, on Apply, calls
patch_scalar(buf, offset, fmt, value). Asset paths use the dedicated string
blob patcher; other strings and structural offset/count fields remain read-only.

Save / repack (mirrors tlb_encode + tlb_viewer):
  * ensure_backup()/restore_backup() keep an untouched <name>_origin.vfxb.
  * pack_diff() copies the saved file into a PackBack tree (preserving its path
    relative to the Output root) and runs FF16Tools.CLI to build 0029.diff.pac.
"""

import struct
import shutil
import subprocess
from pathlib import Path

from path_config import configured_path, load_config


BLOB_PTR_BASE = 0x1B8
BLOB_0X2_PTR = BLOB_PTR_BASE + 0x14
ITEM_BLOB_PTR = BLOB_PTR_BASE + 0x1C

# Repack paths are read from config.json when pack_diff() is called.


# ─────────────────────────────────────────────── scalar patching

# struct format code -> (python packfmt, size, value kind, int range)
_FMT = {
    "B": ("<B", 1, "int",   (0, 0xFF)),
    "h": ("<h", 2, "int",   (-0x8000, 0x7FFF)),
    "H": ("<H", 2, "int",   (0, 0xFFFF)),
    "i": ("<i", 4, "int",   (-0x80000000, 0x7FFFFFFF)),
    "I": ("<I", 4, "int",   (0, 0xFFFFFFFF)),
    "f": ("<f", 4, "float", None),
    "d": ("<d", 8, "float", None),
}


def fmt_kind(fmt):
    """Return 'int' or 'float' for a format code (bitfield specs are int)."""
    if ":" in fmt:
        return "int"
    return _FMT[fmt][2]


def read_scalar(buf, offset, fmt):
    if ":" in fmt:
        base, shift, width = fmt.split(":")
        shift, width = int(shift), int(width)
        cur = struct.unpack_from(_FMT[base][0], buf, offset)[0]
        return (cur >> shift) & ((1 << width) - 1)
    packfmt = _FMT[fmt][0]
    return struct.unpack_from(packfmt, buf, offset)[0]


def parse_value(fmt, text):
    """Parse a user string into a value valid for `fmt`. Raises ValueError."""
    text = text.strip()
    if ":" in fmt:  # bitfield: unsigned, must fit in `width` bits
        _base, _shift, width = fmt.split(":")
        width = int(width)
        val = int(text, 0) if text.lower().startswith("0x") else int(text)
        if not (0 <= val < (1 << width)):
            raise ValueError(f"value {val} does not fit in {width} bits")
        return val
    packfmt, _size, kind, rng = _FMT[fmt]
    if kind == "float":
        return float(text)
    # int
    val = int(text, 0) if text.lower().startswith(("0x", "-0x")) else int(text)
    lo, hi = rng
    if not (lo <= val <= hi):
        raise ValueError(f"value {val} out of range [{lo}, {hi}] for {fmt}")
    return val


def patch_scalar(buf, offset, fmt, value):
    """Write `value` at `offset` in the bytearray `buf` using format `fmt`.

    `fmt` may be a plain code (e.g. "f", "i", "H") or a bitfield spec of the form
    "<base>:<shift>:<width>" — e.g. "H:6:10" writes a 10-bit field starting at
    bit 6 of the u16 at `offset`, preserving the other bits. Used for packed
    header fields like the spawn edge's field_0x4a (slot in bits 0-5, f4a in
    bits 6-15 of a single u16).
    """
    if ":" in fmt:
        base, shift, width = fmt.split(":")
        shift, width = int(shift), int(width)
        packfmt, size = _FMT[base][0], _FMT[base][1]
        if offset < 0 or offset + size > len(buf):
            raise ValueError(f"offset {offset} out of range for <{fmt}>")
        cur = struct.unpack_from(packfmt, buf, offset)[0]
        mask = ((1 << width) - 1) << shift
        if not (0 <= value < (1 << width)):
            raise ValueError(f"value {value} does not fit in {width} bits")
        cur = (cur & ~mask) | ((value << shift) & mask)
        struct.pack_into(packfmt, buf, offset, cur)
        return
    packfmt = _FMT[fmt][0]
    if offset < 0 or offset + _FMT[fmt][1] > len(buf):
        raise ValueError(f"offset {offset} out of range for <{fmt}>")
    struct.pack_into(packfmt, buf, offset, value)


# ─────────────────────────────────────────────── asset-string patching

def _align_up(value, alignment):
    return (value + alignment - 1) // alignment * alignment


def _validate_asset_path(old_value, new_value):
    if not isinstance(new_value, str):
        raise ValueError("asset path must be text")
    if not new_value:
        raise ValueError("asset path cannot be empty")
    if "\x00" in new_value:
        raise ValueError("asset path cannot contain a NUL byte")
    old_ext = Path(old_value).suffix.lower()
    new_ext = Path(new_value).suffix.lower()
    if old_ext not in (".tex", ".mdl"):
        raise ValueError(f"string is not an editable asset path: {old_value!r}")
    if new_ext != old_ext:
        raise ValueError(f"replacement must keep the {old_ext} extension")
    try:
        return new_value.encode("utf-8")
    except UnicodeEncodeError as e:
        raise ValueError(f"asset path is not valid UTF-8: {e}") from e


def patch_asset_strings(buf, decoded, replacements):
    """Replace one or more ``.tex``/``.mdl`` entries in a VFXB string table.

    ``replacements`` maps a string-table index to its new path. Rebuilding all
    entries in one pass keeps texture string indices stable. The space between
    the string blob and ``blob_0x2`` is treated as the blob allocation. Shorter
    strings reuse that allocation; growth is rounded to 16 bytes and shifts the
    two following blobs, whose absolute pointer-table addresses are updated.

    Returns the file-size delta (zero unless the allocation had to grow).
    """
    if not isinstance(buf, bytearray):
        raise TypeError("buf must be a bytearray")
    if not replacements:
        return 0

    entries = decoded.get("string_entries")
    if entries is None:
        strings = decoded.get("strings", [])
        entries = [{"index": i, "value": value}
                   for i, value in enumerate(strings)]
    values = [entry["value"] for entry in entries]
    expected_count = decoded.get("header", {}).get("string_count", len(values))
    if len(values) != expected_count:
        raise ValueError("decoded string table count is inconsistent")

    for index, new_value in replacements.items():
        if not isinstance(index, int) or not (0 <= index < len(values)):
            raise ValueError(f"string index out of range: {index!r}")
        _validate_asset_path(values[index], new_value)
        values[index] = new_value

    raw = b"".join(value.encode("utf-8") + b"\x00" for value in values)
    blobs = decoded["blob_pointers"]
    string_addr = blobs["string_blob"]["address"]
    blob2_addr = blobs["blob_0x2"]["address"]
    item_addr = blobs["item_blob"]["address"]
    if not (0 <= string_addr <= blob2_addr <= item_addr <= len(buf)):
        raise ValueError("invalid VFXB blob ordering")

    old_span = blob2_addr - string_addr
    if len(raw) <= old_span:
        new_span = old_span
    else:
        # Major blobs are aligned in observed VFXB files. Growing by a whole
        # alignment unit retains the original alignment of both later blobs.
        new_span = old_span + _align_up(len(raw) - old_span, 0x10)
    replacement_blob = raw + b"\x00" * (new_span - len(raw))
    buf[string_addr:blob2_addr] = replacement_blob

    delta = new_span - old_span
    if delta:
        struct.pack_into("<I", buf, BLOB_0X2_PTR, blob2_addr + delta)
        struct.pack_into("<I", buf, ITEM_BLOB_PTR, item_addr + delta)
    return delta


# ─────────────────────────────────────────────── editable-field extraction
#
# Walk a decoded node and produce the same "sections" shape tlb_encode uses so
# the viewer's inspector can render one editor per value:
#   [{"title": str, "fields": [{key, offset, fmt, value}, ...]}, ...]
# `offset` is the absolute byte position into the file buffer.


def _field(key, offset, fmt, value):
    return {"key": key, "offset": offset, "fmt": fmt, "value": value}


def build_node_fields(node):
    """Return editable-field sections for one decoded item node.

    Only values whose absolute byte offset the decoder recorded are emitted;
    everything else (structural offsets, strings, texture groups) is skipped.
    """
    sections = []

    # ── lifetime / timeline (item-header floats) ─────────────────────────────
    # duration (0x0C) = the length of this node's bar in the editor timeline;
    # lifetime_base (0x10) + lifetime_range (0x14) are the editor's "lifetime"
    # and "lifetime range" inputs. Each *_addr is an absolute file offset.
    life = []
    for key, addr_key, val_key in (
        ("duration", "duration_addr", "duration"),
        ("lifetime base", "lifetime_base_addr", "lifetime_base"),
        ("lifetime range", "lifetime_range_addr", "lifetime_range"),
    ):
        addr = node.get(addr_key)
        val = node.get(val_key)
        if addr is not None and isinstance(val, (int, float)):
            life.append(_field(key, addr, "f", val))
    if life:
        sections.append({"title": "Lifetime / timeline", "fields": life})

    props = node.get("properties", [])

    # ── spawn edges: editable interval / count / f8 / f9 curve values ────────
    # Each 0x2B edge has up to four param curves (interval, count, f8, f9). Their
    # values live at a recorded absolute address, so each key is patchable in
    # place — this lets you tweak a spawn interval/count and test it in-game.
    edges = [p for p in props if p.get("kind") == "item_group"]
    for ei, p in enumerate(edges):
        fields = []
        # header integers — spawn burst count + the still-unidentified f4a/f5.
        # These are prime suspects for spawn mode / max-at-once, so they are made
        # editable to probe their effect in-game. f4a is a 10-bit field packed
        # with `slot` in a u16, so it uses a bitfield format spec.
        cnt_addr = p.get("spawn_count_addr")
        if cnt_addr is not None and isinstance(p.get("spawn_count"), int):
            fields.append(_field("spawn_count (burst)", cnt_addr, "i",
                                 p["spawn_count"]))
        f4a_addr = p.get("packed_f4_addr")
        if f4a_addr is not None and isinstance(p.get("field_0x4a"), int):
            fields.append(_field("field_0x4a", f4a_addr, "H:6:10",
                                 p["field_0x4a"]))
        f5_addr = p.get("field_0x5_addr")
        if f5_addr is not None and isinstance(p.get("field_0x5"), int):
            fields.append(_field("field_0x5", f5_addr, "h", p["field_0x5"]))
        # curve values (interval / count / f8 / f9), patchable in place
        for label, ck in (("interval", "interval_curve"),
                          ("count", "count_curve"),
                          ("f8", "f8_curve"), ("f9", "f9_curve")):
            curve = p.get(ck)
            if not (curve and curve.get("keys")):
                continue
            keys = curve["keys"]
            for ki, k in enumerate(keys):
                addr = k.get("value_addr")
                val = k.get("value")
                if addr is not None and isinstance(val, (int, float)):
                    name = label if len(keys) == 1 else f"{label}[{ki}]"
                    fields.append(_field(name, addr, "f", val))
        if fields:
            sections.append({"title": f"Spawn edge {ei}", "fields": fields})

    scalars = [p for p in props if p.get("kind") in ("scalar", "scalar2")]
    curves = [p for p in props if p.get("kind") == "keylist"]

    # ── scalar / scalar2 properties ──────────────────────────────────────
    if scalars:
        fields = []
        for i, p in enumerate(scalars):
            tag = p.get("target_offset")
            tag = f"@{tag}" if tag is not None else f"#{i}"
            if p.get("kind") == "scalar":
                addr = p.get("value_addr")
                if addr is not None and isinstance(p.get("value"), (int, float)):
                    fields.append(_field(f"{p['type']} {tag}", addr, "f",
                                         p["value"]))
            else:  # scalar2
                addrs = p.get("value_addrs") or []
                vals = p.get("values") or []
                for j, (a, val) in enumerate(zip(addrs, vals)):
                    if a is not None and isinstance(val, (int, float)):
                        fields.append(_field(f"{p['type']} {tag}[{j}]", a, "f",
                                             val))
        if fields:
            sections.append({"title": "Scalars", "fields": fields})

    # ── animation curves: one section per curve, one editor per keyframe ──
    for ci, p in enumerate(curves):
        fields = []
        tgt = p.get("target_offset")
        tag = f"@{tgt}" if tgt is not None else ""
        for ki, k in enumerate(p["keys"]["keys"]):
            t_off = k.get("time_offset")
            if t_off is not None and isinstance(k.get("time"), (int, float)):
                fields.append(_field(f"key[{ki}] time", t_off, "f", k["time"]))
            v_off = k.get("value_offset")
            if v_off is not None and isinstance(k.get("value"), (int, float)):
                fields.append(_field(f"key[{ki}] value", v_off, "f", k["value"]))
            tans = k.get("tangents")
            toffs = k.get("tangent_offsets")
            if tans and toffs:
                for lbl, to, tv in zip(("tan in", "tan out"), toffs, tans):
                    if isinstance(tv, (int, float)):
                        fields.append(
                            _field(f"key[{ki}] {lbl}", to, "f", tv))
        if fields:
            title = f"Curve {ci}  {p['type']} {tag}".strip()
            sections.append({"title": title, "fields": fields})

    return sections


def build_constant_fields(constants):
    """Return editable-field sections for the shader-constant table.

    kFloat32 constants get one float editor; kVector4 get four; other types are
    shown but skipped (not scalar-patchable in place).
    """
    fields = []
    for i, c in enumerate(constants):
        addr = c.get("value_addr")
        fmt = c.get("value_fmt")
        if addr is None:
            continue
        name = c.get("name", f"const{i}")
        short = name.split("/")[-1]
        if fmt == "f":
            fields.append(_field(f"[{i}] {short}", addr, "f", c["value"]))
        elif fmt == "4f":
            for j, v in enumerate(c["value"]):
                fields.append(_field(f"[{i}] {short}.{'xyzw'[j]}",
                                     addr + j * 4, "f", v))
        # kInt / other: not offered (structural / index-like)
    if fields:
        return [{"title": "Shader constants", "fields": fields}]
    return []


# ─────────────────────────────────────────────── backup / restore

def origin_path(path):
    """foo.vfxb -> foo_origin.vfxb (sibling backup of the untouched original)."""
    p = Path(path)
    return p.with_name(f"{p.stem}_origin{p.suffix}")


def ensure_backup(path):
    """Copy `path` to its *_origin.vfxb sibling once, if not already present.

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
    """Copy the *_origin.vfxb back over `path`. Returns True if a backup existed."""
    bak = origin_path(path)
    if not bak.exists():
        return False
    shutil.copy2(bak, Path(path))
    return True


# ─────────────────────────────────────────────── PackBack + repack

def pack_diff(saved_path):
    """Mirror `saved_path` into PackBack and build the .diff.pac archive.

    Returns (ok, message). Non-fatal: the file is already saved in place, so a
    packing failure only means the mod archive wasn't refreshed.
    """
    src = Path(saved_path)
    try:
        settings = load_config()
        output_root = configured_path(settings, "output_root")
        packback_dir = configured_path(settings, "packback_dir")
        diff_pac = configured_path(settings, "diff_pac")
        cli = configured_path(settings, "ff16tools_cli")
        if not cli.is_file():
            return False, f"FF16Tools CLI not found: {cli}"
        if not src.is_file():
            return False, f"VFXB file not found: {src}"
        if diff_pac.suffix.lower() != ".pac":
            return False, "PAC output must be a .pac file"
        if packback_dir == output_root or output_root in packback_dir.parents:
            return False, "PackBack must be outside the VFXB export root"
        if diff_pac == packback_dir or packback_dir in diff_pac.parents:
            return False, "PAC output must be outside PackBack"
    except (OSError, ValueError) as exc:
        return False, str(exc)
    try:
        rel = src.resolve().relative_to(output_root)
    except ValueError:
        return False, f"{src.name} is outside {output_root} — skipped repack"

    dest = packback_dir / rel
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    except Exception as e:  # noqa: BLE001
        return False, f"PackBack copy failed: {e}"

    # remove stale archive first so the pack always produces a fresh one
    try:
        diff_pac.parent.mkdir(parents=True, exist_ok=True)
        if diff_pac.exists():
            diff_pac.unlink()
    except Exception as e:  # noqa: BLE001
        return False, f"Could not prepare {diff_pac.name}: {e}"

    cmd = [str(cli), "pack",
           "-i", str(packback_dir), "-o", str(diff_pac)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              cwd=str(packback_dir.parent))
    except Exception as e:  # noqa: BLE001
        return False, f"{e}\n\n{' '.join(cmd)}"

    if proc.returncode != 0 or not diff_pac.exists():
        tail = (proc.stderr or proc.stdout or "").strip()[-2000:]
        return False, (f"FF16Tools.CLI exited with code {proc.returncode}.\n\n"
                       f"{tail}")

    return True, f"Saved {src.name} → PackBack\\{rel}  ·  packed {diff_pac.name}"
