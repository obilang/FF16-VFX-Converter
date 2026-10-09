"""Build, smoke-test and ZIP a portable Windows x64 release."""

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def main():
    if sys.platform != 'win32' or struct.calcsize('P') != 8 or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise SystemExit('Build with Windows x64 Python. Cross-compilation is not supported.')
    build_env = os.environ.copy()
    build_env['PYTHONUSERBASE'] = str(ROOT / 'build' / 'python-user')
    build_env['PYINSTALLER_CONFIG_DIR'] = str(ROOT / 'build' / 'pyinstaller-cache')
    # Avoid collecting unrelated DLLs (e.g. another application's ICU/OpenSSL)
    # from the developer's PATH instead of the Windows/Qt dependencies.
    build_env['PATH'] = os.pathsep.join((str(Path(sys.executable).parent),
                                       sys.base_prefix,
                                       str(Path(os.environ['SystemRoot']) / 'System32'),
                                       os.environ['SystemRoot']))
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm',
                    str(ROOT / 'VFXBViewer.spec')], cwd=ROOT, env=build_env, check=True)
    bundle = ROOT / 'dist' / 'VFXBViewer'
    for name in ('README.md', 'LICENSE', 'config.example.json'):
        shutil.copy2(ROOT / name, bundle / name)
    # Keep dependency license notices in the distributable, beside the app license.
    licenses = bundle / 'licenses'
    for package in ('PySide6', 'PySide6_Essentials', 'shiboken6'):
        distribution = importlib.metadata.distribution(package)
        for file in distribution.files or []:
            if 'licenses' in file.parts:
                relative = Path(*file.parts[file.parts.index('licenses') + 1:])
                target = licenses / package / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(distribution.locate_file(file), target)
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.is_file():
        licenses.mkdir(parents=True, exist_ok=True)
        shutil.copy2(python_license, licenses / 'Python-LICENSE.txt')

    report = ROOT / 'build' / 'release-smoke.json'
    env = os.environ.copy()
    env['QT_QPA_PLATFORM'] = 'offscreen'
    # Launch the packaged program from an unrelated directory, without Python on PATH.
    env['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
    env.pop('PYTHONHOME', None)
    env.pop('PYTHONPATH', None)
    subprocess.run([str(bundle / 'VFXBViewer.exe'), '--smoke-test', str(report)],
                   cwd=os.environ.get('TEMP', str(ROOT)), env=env, check=True,
                   timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
    result = json.loads(report.read_text(encoding='utf-8'))
    if not result.get('ok'):
        raise SystemExit(f'Release smoke test failed: {result}')
    archive = Path(shutil.make_archive(str(ROOT / 'dist' / 'VFXBViewer-windows-x64'),
                                      'zip', root_dir=bundle.parent, base_dir=bundle.name))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{digest}  {archive.name}\n', encoding='ascii')
    print(f'Release verified: {archive} ({archive.stat().st_size / 1024 / 1024:.1f} MiB)')


if __name__ == '__main__':
    main()
