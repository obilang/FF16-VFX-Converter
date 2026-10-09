"""
TEC shader extractor for Final Fantasy XVI
Based on KillzXGaming's TEC.bt 010 Editor template.

Usage:
  python extract_shaders.py                        # process sharpen.tec next to this script
  python extract_shaders.py <file.tec>              # process one file
  python extract_shaders.py <folder/>               # process all .tec files in folder
  python extract_shaders.py <file.tec> <out_dir>    # custom output directory

Output: one .dxbc file per shader blob, plus a summary printed to stdout.
Feed the .dxbc files to  dxc -dumpbin shader.dxbc  to get HLSL disassembly.
"""

import os
import struct
import sys

# ---------------------------------------------------------------------------
# Layout constants (derived from TEC.bt)
# ---------------------------------------------------------------------------

# ShaderHeader = char[4] + 27 × uint32  =  4 + 108  =  112 bytes
SHADER_HEADER_FMT  = '<4s27I'
SHADER_HEADER_SIZE = struct.calcsize(SHADER_HEADER_FMT)   # 112

# After reading ShaderHeader, 010 Editor captures pos = FTell() = 112.
# Every offset stored in ShaderHeader is relative to pos (i.e. += 112).
POS = SHADER_HEADER_SIZE   # 112

# ShaderData on-disk: uint32 + uint32 + uint32 + uint8 + uint8 + uint16 = 16 bytes
SHADER_DATA_ENTRY_FMT  = '<IIIBBB'   # last field is uint16, read separately
SHADER_DATA_ENTRY_SIZE = 16

# ShaderDefine on-disk: uint16 + uint16 = 4 bytes
SHADER_DEFINE_SIZE = 4

# ShaderDefineInfo on-disk = 56 bytes (see template)
SHADER_DEFINE_INFO_SIZE = 56
# offsets within the 56-byte ShaderDefineInfo block
_SDI_NUM_BLOCKS_OFF   = 48   # byte numBlocks
_SDI_NUM_UNIFORMS_OFF = 49   # byte numUniforms
_SDI_NUM_SAMPLERS_OFF = 50   # byte numSamplers

# Uniform on-disk: uint8 index + uint8 flag + uint16 nameOffset = 4 bytes
UNIFORM_ENTRY_SIZE = 4

# ---------------------------------------------------------------------------
# DXBC resource-binding constants (D3D11 / SM5)
# ---------------------------------------------------------------------------

_D3D_SIT = {
    0: 'cbuffer',       1: 'tbuffer',       2: 'texture',      3: 'sampler',
    4: 'uav_rw',        5: 'structured',    6: 'uav_struct',   7: 'byteaddr',
    8: 'uav_byteaddr',  9: 'uav_append',   10: 'uav_consume', 11: 'uav_rwstruct',
}

_D3D_SRV_DIM = {
    0: 'unknown',   1: 'Buffer',          2: 'Texture1D',        3: 'Texture1DArray',
    4: 'Texture2D', 5: 'Texture2DArray',  6: 'Texture2DMS',      7: 'Texture2DMSArray',
    8: 'Texture3D', 9: 'TextureCube',    10: 'TextureCubeArray', 11: 'BufferEx',
}

_BINDLESS = 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_cstring(data: bytes, offset: int) -> str:
    end = data.index(b'\x00', offset)
    return data[offset:end].decode('utf-8', errors='replace')


def read_uniform(data: bytes, abs_offset: int, str_base: int) -> dict:
    """Read a single Uniform entry (4 bytes) and resolve its name."""
    index, flag, name_off = struct.unpack_from('<BBH', data, abs_offset)
    name = read_cstring(data, str_base + name_off)
    return {'index': index, 'flag': flag, 'name': name}


# ---------------------------------------------------------------------------
# Core parser
# ---------------------------------------------------------------------------

def parse_tec(filepath: str, output_dir: str | None = None) -> int:
    if output_dir is None:
        output_dir = os.path.splitext(filepath)[0] + '_shaders'
    os.makedirs(output_dir, exist_ok=True)

    with open(filepath, 'rb') as fh:
        data = fh.read()

    if len(data) < SHADER_HEADER_SIZE:
        print(f'[!] File too small: {filepath}')
        return 0

    # --- Unpack ShaderHeader ---
    (magic,
     flags, chunk_size, padd,
     unkOffset1, unkCount1,
     shaderProgramOffset, shaderProgramCount,
     unkShaderOffset, unkShaderCount,
     indicesOffset, indicesCount,
     shaderDataTableOffset, shaderCount,
     shaderDefineOffset, shaderDefineCount,
     sectionSize, shaderBlobAreaSize,
     stringTableOffset, stringTableSize,
     shaderDefineInfoOffset, uniformBufferOffset,
     samplerCount, samplerNameOffset, samplerConfigOffset, unk_last,
     pad1, pad2) = struct.unpack_from(SHADER_HEADER_FMT, data, 0)

    if magic[:3] != b'TEC':
        print(f'[!] Unexpected magic {magic!r} in {filepath}')

    str_base = POS + stringTableOffset   # absolute offset to string table

    print(f'{"="*60}')
    print(f'File    : {os.path.basename(filepath)}')
    print(f'Shaders : {shaderCount}')
    print(f'Samplers: {samplerCount}')
    print(f'SectionSize (metadata end): {sectionSize:#010x}  '
          f'=> shader blobs start at {POS + sectionSize:#010x}')

    # --- Sampler names ---
    if samplerCount > 0:
        name_tbl = POS + samplerNameOffset
        names = []
        for i in range(samplerCount):
            name_off = struct.unpack_from('<H', data, name_tbl + i * 2)[0]
            names.append(read_cstring(data, str_base + name_off))
        print(f'Samplers: {", ".join(names)}')

    # --- Extract shader blobs ---
    tbl_base = POS + shaderDataTableOffset
    extracted = 0

    for i in range(shaderCount):
        entry = tbl_base + i * SHADER_DATA_ENTRY_SIZE
        blob_rel, blob_size, define_idx = struct.unpack_from('<III', data, entry)
        unk_b1, unk_b2 = struct.unpack_from('<BB', data, entry + 12)

        if blob_size == 0:
            continue

        blob_abs = POS + sectionSize + blob_rel
        blob_end  = blob_abs + blob_size

        if blob_end > len(data):
            print(f'  [{i:4d}] ERROR: blob extends past EOF '
                  f'(needs {blob_end}, file is {len(data)})')
            continue

        blob = data[blob_abs:blob_end]

        # Determine format by magic
        blob_magic = blob[:4]
        if blob_magic == b'DXBC':
            ext = 'dxbc'
        elif blob_magic == b'DXIL':
            ext = 'dxil'
        else:
            ext = 'bin'

        # unk_b1 = 0 or 1 (stage group?), unk_b2 = often 2 (stage type?)
        out_name = f'shader_{i:04d}_u{unk_b1}_u{unk_b2}.{ext}'
        out_path = os.path.join(output_dir, out_name)
        with open(out_path, 'wb') as fh:
            fh.write(blob)

        # Optionally read shader symbol info from its ShaderDefine
        symbols = _read_symbols(data, define_idx, shaderDefineOffset,
                                shaderDefineInfoOffset, uniformBufferOffset,
                                str_base)

        print(f'  [{i:4d}] @{blob_abs:#010x}  {blob_size:8d} B  '
              f'{blob_magic!r}  {out_name}'
              + (f'  blocks={symbols["blocks"]}  uniforms={symbols["uniforms"]}'
                 f'  samplers={symbols["samplers"]}'
                 if symbols else ''))

        # Print Texture2DArray bindings (change filter_dim or drop it to see all resources)
        print_shader_resources(blob)
        extracted += 1

    print(f'\nExtracted {extracted}/{shaderCount} shaders -> {output_dir}\n')
    return extracted


def _read_symbols(data, define_idx, shaderDefineOffset,
                  shaderDefineInfoOffset, uniformBufferOffset, str_base):
    """
    Follow ShaderData -> ShaderDefine -> ShaderDefineInfo -> Uniform lists.
    Returns dict with keys 'blocks', 'uniforms', 'samplers' (lists of names),
    or None on any parse error.
    """
    try:
        # ShaderDefine location: pos + shaderDefineOffset + define_idx * 4
        sd_abs = POS + shaderDefineOffset + define_idx * 4
        sdi_idx, uniform_str_start = struct.unpack_from('<HH', data, sd_abs)

        # ShaderDefineInfo location: pos + shaderDefineInfoOffset + sdi_idx * 56
        sdi_abs = POS + shaderDefineInfoOffset + sdi_idx * SHADER_DEFINE_INFO_SIZE

        num_blocks   = data[sdi_abs + _SDI_NUM_BLOCKS_OFF] & 0x0F
        num_uniforms = data[sdi_abs + _SDI_NUM_UNIFORMS_OFF]
        num_samplers = data[sdi_abs + _SDI_NUM_SAMPLERS_OFF]

        # Uniform entries: pos + uniformBufferOffset + uniformStringStart * 4
        uni_base = POS + uniformBufferOffset + uniform_str_start * UNIFORM_ENTRY_SIZE

        def read_n(start, count):
            return [read_uniform(data, start + j * UNIFORM_ENTRY_SIZE, str_base)['name']
                    for j in range(count)]

        blocks   = read_n(uni_base, num_blocks)
        uniforms = read_n(uni_base + num_blocks   * UNIFORM_ENTRY_SIZE, num_uniforms)
        samplers = read_n(uni_base + (num_blocks + num_uniforms) * UNIFORM_ENTRY_SIZE,
                          num_samplers)

        return {'blocks': blocks, 'uniforms': uniforms, 'samplers': samplers}
    except Exception:
        return None


def parse_dxbc_resources(blob: bytes) -> list | None:
    """
    Parse the RDEF chunk of a DXBC blob and return a list of resource binding dicts.
    Each dict has: name, sit (input type str), dim (dimension str),
                   bind_point, bind_count, bindless (bool).
    Returns None if blob is not valid DXBC or has no RDEF chunk.
    """
    if len(blob) < 32 or blob[:4] != b'DXBC':
        return None

    # DXBC container: magic[4] + checksum[16] + version[4] + total_size[4] + chunk_count[4]
    # Chunk offset table starts at byte 32.
    try:
        chunk_count = struct.unpack_from('<I', blob, 28)[0]
    except struct.error:
        return None

    rdef_off = None
    for i in range(chunk_count):
        ptr = 32 + i * 4
        if ptr + 4 > len(blob):
            break
        chunk_start = struct.unpack_from('<I', blob, ptr)[0]
        if chunk_start + 8 > len(blob):
            break
        if blob[chunk_start:chunk_start + 4] == b'RDEF':
            rdef_off = chunk_start
            break

    if rdef_off is None:
        return None

    # RDEF data begins 8 bytes after the chunk start (tag[4] + size[4])
    rd = rdef_off + 8

    if rd + 16 > len(blob):
        return None

    # RDEF header: cbuf_count, cbuf_off, res_count, res_off (all uint32, relative to rd)
    _cb_count, _cb_off, res_count, res_off = struct.unpack_from('<IIII', blob, rd)

    resources = []
    entry_base = rd + res_off
    # Each D3D11_SHADER_INPUT_BIND_DESC = 32 bytes
    for i in range(res_count):
        e = entry_base + i * 32
        if e + 32 > len(blob):
            break
        name_off, sit, _ret, dim, _samples, bind_pt, bind_count, _flags = \
            struct.unpack_from('<IIIIIIII', blob, e)
        name = read_cstring(blob, rd + name_off)
        resources.append({
            'name':       name,
            'sit':        _D3D_SIT.get(sit,  f'sit_{sit}'),
            'dim':        _D3D_SRV_DIM.get(dim, f'dim_{dim}'),
            'bind_point': bind_pt,
            'bind_count': bind_count,
            'bindless':   bind_count == _BINDLESS,
        })

    return resources


def print_shader_resources(blob: bytes, *,
                           bindless_only: bool = False,
                           filter_dim: str | None = None) -> None:
    """
    Print resource bindings embedded in a DXBC blob.
    bindless_only : only show resources with unbounded bind count (0xFFFFFFFF).
    filter_dim    : only show resources whose dimension matches this string
                    (e.g. 'Texture2DArray', 'Texture2D').  Case-sensitive.
    If both flags are set, a resource must satisfy both conditions.
    Returns silently if blob has no RDEF chunk (stripped shaders).
    """
    resources = parse_dxbc_resources(blob)
    if resources is None:
        return  # RDEF chunk absent (stripped)
    if not resources:
        return

    for r in resources:
        if bindless_only and not r['bindless']:
            continue
        if filter_dim and r['dim'] != filter_dim:
            continue

        count_str = 'unbounded' if r['bindless'] else str(r['bind_count'])
        tag = '  ** BINDLESS **' if r['bindless'] else ''
        print(f'      {r["sit"]:12s}  {r["dim"]:20s}  '
              f't{r["bind_point"]}[{count_str}]  "{r["name"]}"{tag}')

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]

    if not args:
        # Default: look for sharpen.tec next to this script
        here = os.path.dirname(os.path.abspath(__file__))
        targets = [os.path.join(here, r'E:\Workspace\FF16\Output\shader\postprocesseyeadaptation.tec')]
    elif os.path.isdir(args[0]):
        folder = args[0]
        targets = sorted(
            os.path.join(folder, f)
            for f in os.listdir(folder)
            if f.lower().endswith('.tec')
        )
        if not targets:
            print(f'No .tec files found in {folder}')
            sys.exit(1)
    else:
        targets = [args[0]]

    out_dir = args[1] if len(args) > 1 else None

    total = 0
    for tec in targets:
        if not os.path.isfile(tec):
            print(f'File not found: {tec}')
            continue
        total += parse_tec(tec, out_dir)

    if len(targets) > 1:
        print(f'Total extracted across all files: {total}')

    # Remind user how to disassemble
    print('To disassemble a .dxbc file to HLSL:')
    print('  dxc.exe -dumpbin shader_0000_u0_u2.dxbc')
    print('  (install dxc: winget install Microsoft.DirectXShaderCompiler)')


if __name__ == '__main__':
    main()
