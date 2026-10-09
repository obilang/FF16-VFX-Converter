"""
FF16 .vatb (VFX Audio Table Binary) decoder.

Ports the 010 Editor template VFX/FF16_vatb_VFXAudioTable.bt to Python and
emits a JSON manifest listing every VFX/audio pairing in the table.

Format layout
-------------
Offset 0x00  int32  HeaderSize      -- byte offset to the first entry
Offset 0x04  int32  EntryCount      -- number of 0x20-byte entries

Entries (each 0x20 bytes, base = HeaderSize + i * 0x20):
  +0x00  int32  ID
  +0x04  int32  NameOffset          -- cstring at (entry_base + NameOffset)
  +0x08  int32  VFXFileOffset       -- KeyPathPair at (entry_base + VFXFileOffset)
  +0x0C  int32  field_0x0C
  +0x10  uint64 Flags
  +0x18  int32  AudioFileOffset     -- KeyPathPair at (entry_base + AudioFileOffset)
  +0x1C  int32  field_0x1C

KeyPathPair (at some base address):
  +0x00  int32  Id
  +0x04  int32  PathOffset          -- cstring at (keypair_base + PathOffset)

Usage:
    python vatb_decode.py <input.vatb> [output.json]
    python vatb_decode.py <input.vatb>     # writes <stem>.json next to input
"""

import json
import struct
import sys
from pathlib import Path


def cstr(data, off):
    end = data.index(b"\x00", off)
    return data[off:end].decode("utf-8", "replace")


def parse_keypair(data, base):
    id_, path_off = struct.unpack_from("<ii", data, base)
    path = cstr(data, base + path_off)
    return {"id": id_, "path": path}


def decode(path):
    data = Path(path).read_bytes()

    header_size, entry_count = struct.unpack_from("<ii", data, 0)

    entries = []
    for i in range(entry_count):
        base = header_size + i * 0x20
        id_, name_off, vfx_off, field_0c, flags, audio_off, field_1c = \
            struct.unpack_from("<iiiiQii", data, base)

        name = cstr(data, base + name_off)
        vfx_file = parse_keypair(data, base + vfx_off)
        audio_file = parse_keypair(data, base + audio_off)

        entries.append({
            "id": id_,
            "name": name,
            "flags": flags,
            "field_0x0C": field_0c,
            "field_0x1C": field_1c,
            "vfx_file": vfx_file,
            "audio_file": audio_file,
        })

    return {
        "file": str(path),
        "size": len(data),
        "header_size": header_size,
        "entry_count": entry_count,
        "entries": entries,
    }


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1

    inp = Path(argv[1])
    out = Path(argv[2]) if len(argv) > 2 else inp.with_suffix(".json")

    result = decode(inp)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"Decoded {inp.name} ({result['size']} bytes)")
    print(f"  entries : {result['entry_count']}")
    for e in result["entries"]:
        print(f"  [{e['id']:#06x}] {e['name']}")
        print(f"           vfx  : {e['vfx_file']['path']}")
        print(f"           audio: {e['audio_file']['path']}")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
