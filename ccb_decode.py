"""Decode FF16 CharaCollisionBinary (.ccb, magic CCCB) to JSON.

Based on Nenkai's FF16_ccb_CharaCollisionBinary.bt. Header offsets are
file-relative; entry and EID offsets are relative to their record start.
Unknown fields retain the template's names and signed integer values.
Raw fields are preserved. The optional shape interpretation is inferred from
the c1001 sample, not confirmed against the engine; see CCB_FORMAT.md.

Usage: python ccb_decode.py input.ccb [output.json]
"""

import argparse
import json
import math
import struct
from pathlib import Path

MAGIC = b"CCCB"
HEADER_SIZE = 0x2C
ENTRY_SIZE = 0x5C
EID_SIZE = 0x40
DATA_WORD_COUNTS = {2: 13, 3: 14}
# Number of int32 fields AFTER Type and CollisionId in the supplied template.
# Collision 14 repeats the field_0x0C label, but declares seven words in total.
COLLISION_WORD_COUNTS = {
    1: 5, 2: 14, 3: 2, 4: 2, 5: 8, 6: 2, 7: 2, 8: 12, 9: 2,
    10: 10, 14: 7, 15: 10, 16: 10, 17: 12, 18: 10, 19: 10,
    20: 10, 21: 2, 22: 2, 23: 2, 24: 10, 25: 10, 26: 10,
}


def _json_float(value):
    # Keep exports valid JSON even when arbitrary payload bits encode NaN/Inf.
    return value if math.isfinite(value) else str(value)


class _Reader:
    def __init__(self, data):
        self.data = data

    def check(self, offset, size, label):
        if offset < 0 or size < 0 or offset + size > len(self.data):
            raise ValueError(f"{label}: range 0x{offset:X} + 0x{size:X} "
                             f"exceeds file size 0x{len(self.data):X}")

    def unpack(self, fmt, offset, label):
        self.check(offset, struct.calcsize('<' + fmt), label)
        return struct.unpack_from('<' + fmt, self.data, offset)

    def target(self, base, relative, size, label):
        offset = base + relative
        if offset < HEADER_SIZE:
            raise ValueError(f"{label}: reference points into the header at 0x{offset:X}")
        self.check(offset, size, label)
        return offset

    def string(self, base, relative, label):
        if relative == 0:
            return ""
        offset = self.target(base, relative, 1, label)
        end = self.data.find(b'\0', offset)
        if end < 0:
            raise ValueError(f"{label}: unterminated string at 0x{offset:X}")
        return self.data[offset:end].decode('utf-8', 'replace')

    def table(self, offset, count, stride, label):
        if count:
            self.target(0, offset, count * stride, label)

    def payload(self, base, relative, collision, label, warnings):
        if relative == 0:
            return None
        header_size = 8 if collision else 4
        offset = self.target(base, relative, header_size, label)
        type_, = self.unpack('i', offset, label)
        result = {'offset': offset, 'type': type_}
        if collision:
            discriminator, = self.unpack('i', offset + 4, label)
            result['collision_id'] = discriminator
            count = COLLISION_WORD_COUNTS.get(discriminator)
        else:
            discriminator = type_
            count = DATA_WORD_COUNTS.get(type_)
        result['known_layout'] = count is not None
        if count is None:
            warnings.append(f"{label}: unknown {'collision ID' if collision else 'type'} "
                            f"{discriminator} at 0x{offset:X}; only the header decoded")
            count = 0
        size = header_size + count * 4
        self.check(offset, size, label)
        result['fields'] = {
            f'field_0x{header_size + i * 4:02X}':
                self.unpack('i', offset + header_size + i * 4, label)[0]
            for i in range(count)
        }
        result['float_interpretation'] = {
            f'field_0x{header_size + i * 4:02X}':
                _json_float(self.unpack('f', offset + header_size + i * 4, label)[0])
            for i in range(count)
        }
        result['raw_hex'] = self.data[offset:offset + size].hex()
        return result


def _infer_shape(reader, entry, version):
    """Conservative, explicitly provisional geometry for the observed layout."""
    unavailable = {'status': 'unavailable'}
    payload = entry['data_0x20']
    if version != 1 or not payload or payload['type'] not in (2, 3):
        return {**unavailable, 'reason': 'Unrecognized version or shape payload'}
    type_ = payload['type']
    if type_ != entry['data_0x20_type']:
        return {**unavailable, 'reason': 'Entry and payload type disagree'}
    values = payload['float_interpretation']
    dimension_keys = {'field_0x14'} if type_ == 2 else {'field_0x14', 'field_0x18'}
    if any(value != 0 for key, value in values.items() if key not in dimension_keys):
        return {**unavailable, 'reason': 'Payload differs from the observed zero-filled layout'}
    radius = values['field_0x14']
    half_segment = values['field_0x18'] if type_ == 3 else 0.0
    position = reader.unpack('3f', entry['offset'] + 0x2C, 'shape position')
    rotation = reader.unpack('3f', entry['offset'] + 0x38, 'shape rotation')
    if not all(isinstance(v, (int, float)) and math.isfinite(v)
               for v in (radius, half_segment, *position, *rotation)):
        return {**unavailable, 'reason': 'Non-finite dimensions or transform'}
    if radius <= 0 or half_segment < 0:
        return {**unavailable, 'reason': 'Non-positive radius or negative half-segment length'}
    shape = {
        'status': 'inferred',
        'kind': 'sphere' if type_ == 2 else 'capsule',
        'radius': radius,
        'position': dict(zip('XYZ', position)),
        'rotation_radians': dict(zip('XYZ', rotation)),
        'joint_name': entry['joint_name'],
        'assumptions': [
            'Shape type and dimensions inferred from c1001; engine confirmation pending',
            'Position assumed relative to the named joint; no skeleton transform applied',
            'Rotation appears to be Euler radians; axis/order/handedness are unconfirmed',
            'Units are unconfirmed game units',
        ],
    }
    if type_ == 3:
        shape.update(half_segment_length=half_segment,
                     total_height=2 * (radius + half_segment), assumed_axis='Y')
        shape['assumptions'].append(
            'Second dimension assumed to be half the distance between cap centers')
    return shape


def decode_ccb(data):
    """Decode bytes with bounded reads; malformed references raise ValueError."""
    data = bytes(data)
    reader = _Reader(data)
    header = reader.unpack('4s10I', 0, 'header')
    if header[0] != MAGIC:
        raise ValueError(f"Bad magic: {header[0]!r}, expected {MAGIC!r}")
    _, version, *rest = header
    reserved = rest[:4]
    name_offset, entry_offset, entry_count, eid_offset, eid_count = rest[4:]
    reader.table(entry_offset, entry_count, ENTRY_SIZE, 'entries')
    reader.table(eid_offset, eid_count, EID_SIZE, 'EID definitions')
    warnings = []
    if version != 1:
        warnings.append(f"Unverified version {version}; decoded using the supplied template layout")
    entries = []
    for index in range(entry_count):
        base = entry_offset + index * ENTRY_SIZE
        label = f'entry {index}'
        words = reader.unpack('23I', base, label)
        signed = reader.unpack('23i', base, label)
        entry = {'index': index, 'offset': base, 'id': signed[0]}
        for word, key in ((1, 'debug_name'), (2, 'name'), (9, 'joint_name'), (10, 'name_0x28')):
            entry[key + '_offset'] = words[word]
            entry[key] = reader.string(base, words[word], f'{label} {key}')
        entry.update(unk_type=signed[4], data_0x20_type=signed[7],
                     offset_0x20=words[8], offset_0x54=words[21])
        for word in (3, 5, 6, 15, 16, 17, 18, 19, 20, 22):
            entry[f'field_0x{word * 4:02X}'] = signed[word]
        for word in range(11, 15):
            entry[f'field_0x{word * 4:02X}'] = _json_float(
                reader.unpack('f', base + word * 4, label)[0])
        entry['data_0x20'] = reader.payload(base, words[8], False, label + ' data_0x20', warnings)
        entry['data_0x54'] = reader.payload(base, words[21], True, label + ' data_0x54', warnings)
        if entry['data_0x20'] and entry['data_0x20']['type'] != entry['data_0x20_type']:
            warnings.append(f'{label}: Data0x20Type differs from the payload Type')
        entry['raw_hex'] = data[base:base + ENTRY_SIZE].hex()
        entry['shape'] = _infer_shape(reader, entry, version)
        entries.append(entry)

    eids = []
    for index in range(eid_count):
        base = eid_offset + index * EID_SIZE
        label = f'EID definition {index}'
        eid_id, joint_offset = reader.unpack('iI', base, label)
        eids.append({
            'index': index, 'offset': base, 'eid_id': eid_id,
            'joint_name_offset': joint_offset,
            'joint_name': reader.string(base, joint_offset, label + ' joint_name'),
            'field_0x08': dict(zip('XYZ', map(_json_float, reader.unpack('3f', base + 8, label)))),
            'field_0x14': dict(zip('XYZ', map(_json_float, reader.unpack('3f', base + 20, label)))),
            'field_0x20': reader.unpack('i', base + 32, label)[0],
            'padding_hex': data[base + 36:base + EID_SIZE].hex(),
            'raw_hex': data[base:base + EID_SIZE].hex(),
        })
    return {
        'magic': MAGIC.decode('ascii'), 'version': version, 'size': len(data),
        'reserved': reserved, 'name_offset': name_offset,
        'name': reader.string(0, name_offset, 'name'),
        'entry_offset': entry_offset, 'entry_count': entry_count,
        'eid_def_list_offset': eid_offset, 'eid_def_count': eid_count,
        'entries': entries, 'eid_definitions': eids, 'warnings': warnings,
    }


def decode(path):
    result = decode_ccb(Path(path).read_bytes())
    return {'file': str(path), **result}


def export_json(result, output):
    output = Path(output)
    if output.suffix.lower() == '.ccb' or (
            result.get('file') and output.resolve() == Path(result['file']).resolve()):
        raise ValueError('Choose a JSON output path separate from the CCB source')
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + '\n',
                      encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', nargs='?', type=Path)
    args = parser.parse_args(argv)
    try:
        result = decode(args.input)
        output = args.output or args.input.with_suffix('.json')
        export_json(result, output)
    except (OSError, ValueError) as exc:
        parser.exit(1, f'CCB decode failed: {exc}\n')
    print(f"Decoded {result['name']}: {result['entry_count']} entries, "
          f"{result['eid_def_count']} EID definitions -> {output}")
    for warning in result['warnings']:
        print(f'Warning: {warning}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
