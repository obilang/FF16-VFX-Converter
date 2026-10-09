"""Windowed release entry point, with a non-interactive bundle smoke check."""

import json
import sys
from pathlib import Path


def smoke_test(report_path):
    import struct
    from PySide6 import QtWidgets
    import convert_glsl
    import extract_shaders
    import path_config
    import vfxb_decode
    from path_settings import PathSettingsDialog
    from vfxb_viewer import MainWindow

    app = QtWidgets.QApplication([])
    # Do not restore a developer's recent files during release verification.
    class SmokeWindow(MainWindow):
        def _restore_last_session(self):
            pass

    window = SmokeWindow()
    dialog = PathSettingsDialog(window)
    window.show()
    dialog.show()
    app.processEvents()
    assert struct.calcsize('P') == 8
    assert getattr(sys, 'frozen', False)
    assert path_config.CONFIG_PATH == Path(sys.executable).resolve().with_name('config.json')
    assert not path_config.CONFIG_PATH.exists(), 'Release must not contain personal config'
    assert list(dialog.edits) == list(path_config.PATH_KEYS)
    assert callable(convert_glsl.convert_one)
    assert extract_shaders.SHADER_HEADER_SIZE == 112
    assert callable(vfxb_decode.export_shaders)
    Path(report_path).write_text(json.dumps({
        'ok': True, 'architecture': 'x64',
        'config_path': str(path_config.CONFIG_PATH),
        'gui': 'Main window and path settings initialized',
        'shader_modules': 'Imported from bundle',
    }, indent=2), encoding='utf-8')
    dialog.close()
    window.close()
    return 0


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--smoke-test':
        try:
            sys.exit(smoke_test(sys.argv[2]))
        except Exception:
            import traceback
            Path(sys.argv[2]).write_text(traceback.format_exc(), encoding='utf-8')
            sys.exit(1)
    else:
        from vfxb_viewer import main
        sys.exit(main(sys.argv))
