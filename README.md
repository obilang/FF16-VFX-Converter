# FF16 VFXB tools

## Windows x64 release (no Python installation needed)

Extract **all** files from `VFXBViewer-windows-x64.zip` to a writable folder,
then double-click `VFXBViewer.exe`. Keep the `_internal` folder beside the EXE.
Python and PySide6 are included. No setup is needed to open/edit/save VFXB files;
Save overwrites the open file and keeps its original sibling `_origin.vfxb` backup.

Settings are stored in `config.json` beside the EXE. The release starts without
a personal config; `config.example.json` is an optional reference. PAC packing
still requires FF16Tools CLI, and GLSL conversion requires the two shader tools.
These external executables and game assets are not included.

### Building a release

On Windows, with **x64 Python 3.11** and a virtual environment:

```powershell
python -m pip install -r requirements-build.txt
python build_release.py
```

This produces `dist/VFXBViewer-windows-x64.zip` plus a SHA-256 checksum.
The build script verifies that the bundled GUI and settings open with Python
removed from PATH, that shader modules are bundled, and that no personal
`config.json` is shipped. Build output is ignored by Git. Test on a clean target
PC before publishing; the smoke test does not exercise external game tools.

## Running from source

Run `python vfxb_viewer.py` with PySide6 installed to open the graph editor.

## Paths

Use **Settings → Paths…**, or copy `config.example.json` to `config.json`
beside the scripts and edit it. The viewer, `vfxb_encode.pack_diff()` and
`convert_glsl.py` share this file; neither standalone tool requires the UI.
Local `config.json` is ignored by Git. Settings are read for each operation.

The dialog lists the VFXB export root first, then PAC paths, then shader tools.
Each field has a browse button, a tip and an example.

- `output_root`: root of your exported/extracted game files. For example,
  `D:/FF16/Output/vfx/fire.vfxb` under `D:/FF16/Output` packs as `vfx/fire.vfxb`.
  This controls the archive-relative path; Save still writes the open file in place.
- `packback_dir`: staging tree, outside `output_root`. That example is copied to
  `<packback_dir>/vfx/fire.vfxb`. The CLI packs **all files** in the staging tree.
- `diff_pac`: full output filename, e.g. `D:/FF16/Mods/0029.diff.pac`, outside
  the staging tree. Packing replaces this archive.
- `ff16tools_cli`: full path to `FF16Tools.CLI.exe`.
- `dxbc2spirv`: full path to `dxil-spirv.exe` or `dxbc2spirv.exe`.
- `spirv_cross`: full path to `spirv-cross.exe`.

Paths may be absolute, UNC network paths, or relative to the config directory.
Environment variables and `~` are expanded. In JSON, use forward slashes or
escape backslashes: `"D:/FF16/Output"` or `"D:\\FF16\\Output"`.
A network example is `"\\\\server\\share\\FF16Raw"`.
Leave unused paths blank. Missing PAC/shader settings never prevent plain Save.

## Saving

- **Save** (`Ctrl+S`): writes edited VFXB bytes and creates a sibling
  `*_origin.vfxb` backup once. Does not copy to PackBack or run external tools.
- **Save and Pack** (`Ctrl+Shift+S`): saves first, then stages and packs the
  file. Also works without new edits, so a failed pack can be retried.
  Packing failure leaves the successfully saved VFXB intact.

For standalone conversion: `python convert_glsl.py path/to/shaders --force`.
For packing from Python: `vfxb_encode.pack_diff("path/to/effect.vfxb")` returns
`(ok, message)`. Configure paths before running either operation.
