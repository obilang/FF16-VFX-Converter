"""
Automatic DXBC -> SPIR-V -> GLSL converter for Final Fantasy XVI shaders.

Pipeline per shader:
  1. dxbc2spirv.exe  shader.dxbc  -o  shader.spv      (DXBC  -> SPIR-V)
  2. spirv-cross.exe shader.spv   --output shader.glsl (SPIR-V -> GLSL)

Usage:
  python convert_glsl.py                       # process .dxbc files next to this script
  python convert_glsl.py <file.dxbc>           # one file
  python convert_glsl.py <folder/>             # all .dxbc files in folder (recursive)
  python convert_glsl.py <path> --keep-spv     # keep intermediate .spv files
  python convert_glsl.py <path> --force        # overwrite existing .glsl
"""

import os
import subprocess
import sys

from path_config import configured_path, load_config

# ---------------------------------------------------------------------------
# Tool locations are shared with the viewer through config.json.
# ---------------------------------------------------------------------------

def tool_paths():
    settings = load_config()
    return tuple(str(configured_path(settings, key))
                 for key in ("dxbc2spirv", "spirv_cross"))

# Extra flags passed to spirv-cross (matches the manual command you used)
SPIRV_CROSS_FLAGS = ['--vulkan-semantics']


# ---------------------------------------------------------------------------
# Conversion steps
# ---------------------------------------------------------------------------

def run(cmd: list[str]) -> tuple[bool, str]:
    """Run a command, capturing stdout+stderr. Returns (ok, combined_output)."""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return proc.returncode == 0, proc.stdout
    except FileNotFoundError as exc:
        return False, str(exc)


def convert_one(dxbc_path: str, *, keep_spv: bool, force: bool) -> bool:
    base = os.path.splitext(dxbc_path)[0]
    spv_path = base + '.spv'
    glsl_path = base + '.glsl'

    name = os.path.basename(dxbc_path)

    if os.path.exists(glsl_path) and not force:
        print(f'  [skip] {name}  (.glsl exists, use --force to overwrite)')
        return True

    # Step 1: DXBC -> SPIR-V
    try:
        dxbc2spirv, spirv_cross = tool_paths()
    except (OSError, ValueError) as exc:
        print(f'  [FAIL] {name}: {exc}')
        return False
    ok, out = run([dxbc2spirv, dxbc_path, '--output', spv_path])
    if not ok or not os.path.exists(spv_path):
        print(f'  [FAIL] {name}  dxbc2spirv error:')
        print('    ' + out.strip().replace('\n', '\n    '))
        return False

    # Step 2: SPIR-V -> GLSL
    ok, out = run([spirv_cross, spv_path, *SPIRV_CROSS_FLAGS, '--output', glsl_path])

    if not keep_spv:
        try:
            os.remove(spv_path)
        except OSError:
            pass

    if not ok or not os.path.exists(glsl_path):
        print(f'  [FAIL] {name}  spirv-cross error:')
        print('    ' + out.strip().replace('\n', '\n    '))
        return False

    print(f'  [ ok ] {name}  ->  {os.path.basename(glsl_path)}')
    return True


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def gather_targets(path: str) -> list[str]:
    if os.path.isdir(path):
        found = []
        for root, _dirs, files in os.walk(path):
            for f in files:
                if f.lower().endswith('.dxbc'):
                    found.append(os.path.join(root, f))
        return sorted(found)
    return [path]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    flags = {a for a in sys.argv[1:] if a.startswith('--')}
    keep_spv = '--keep-spv' in flags
    force = '--force' in flags

    # Validate tools exist up front
    try:
        paths = tool_paths()
    except (OSError, ValueError) as exc:
        print(f'[!] {exc}')
        sys.exit(1)
    for tool in paths:
        if not os.path.isfile(tool):
            print(f'[!] Tool not found: {tool}')
            print('    Set paths in config.json or the viewer Settings > Paths menu.')
            sys.exit(1)

    if args:
        path = args[0]
    else:
        path = os.path.dirname(os.path.abspath(__file__))

    if not os.path.exists(path):
        print(f'Path not found: {path}')
        sys.exit(1)

    targets = gather_targets(path)
    if not targets:
        print(f'No .dxbc files found in {path}')
        sys.exit(1)

    print(f'Converting {len(targets)} shader(s)...\n')
    ok_count = 0
    for dxbc in targets:
        if convert_one(dxbc, keep_spv=keep_spv, force=force):
            ok_count += 1

    print(f'\nDone: {ok_count}/{len(targets)} converted to GLSL.')
    if ok_count != len(targets):
        sys.exit(1)


if __name__ == '__main__':
    main()
