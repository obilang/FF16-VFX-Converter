"""Path settings UI; command-line tools only import path_config."""

from PySide6 import QtCore, QtWidgets

import path_config


class PathSettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, *, config_path=path_config.CONFIG_PATH,
                 keys=path_config.PATH_KEYS, file_type="VFXB",
                 example_rel="vfx/fire.vfxb"):
        super().__init__(parent)
        self.setWindowTitle("Path settings")
        self.resize(780, 720)
        self._config_path = config_path
        self._keys = keys
        settings = path_config.load_config(config_path, keys)
        layout = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            f"Config: {config_path}\n"
            "Absolute paths and UNC network paths are supported. Relative paths "
            "start beside config.json, regardless of the working directory. "
            "Blank paths disable the corresponding operation; Save still works.")
        intro.setWordWrap(True)
        intro.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(intro)
        self.edits = {}
        groups = [
            (f"{file_type} export path", [
                ("output_root", "Export / extracted root", "directory",
                 "D:/FF16/Output",
                 f"Root of the extracted game files. Save writes the opened {file_type} in place. "
                 f"For D:/FF16/Output/{example_rel}, this root makes its PAC path {example_rel}.")]),
            ("PAC packing", [
                ("packback_dir", "PackBack folder", "directory",
                 "D:/FF16/PackBack/Effects",
                 "Staging folder outside the export root. The example above is copied to "
                 f"D:/FF16/PackBack/Effects/{example_rel}. All files in this folder are packed."),
                ("diff_pac", "PAC output file", "save",
                 "D:/FF16/Mods/0029.diff.pac",
                 "Full .pac filename, outside PackBack. Save and Pack replaces this archive."),
                ("ff16tools_cli", "FF16Tools CLI", "file",
                 "D:/Tools/FF16Tools/FF16Tools.CLI.exe",
                 "Select the executable, not its folder. Only needed for packing.")]),
            ("Shader conversion (optional)", [
                ("dxbc2spirv", "DXBC → SPIR-V tool", "file",
                 "D:/Tools/dxil-spirv/dxil-spirv.exe",
                 "Full path to the DXBC-to-SPIR-V executable (dxil-spirv.exe or dxbc2spirv.exe)."),
                ("spirv_cross", "SPIRV-Cross", "file",
                 "D:/Tools/SPIRV-Cross/spirv-cross.exe",
                 "Full path to spirv-cross.exe. Only needed when converting shaders to GLSL.")]),
        ]
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        content = QtWidgets.QWidget()
        fields = QtWidgets.QVBoxLayout(content)
        for title, rows in groups:
            rows = [row for row in rows if row[0] in keys]
            if not rows:
                continue
            group = QtWidgets.QGroupBox(title)
            form = QtWidgets.QFormLayout(group)
            for key, label, kind, sample, tip in rows:
                edit = QtWidgets.QLineEdit(settings[key])
                edit.setPlaceholderText(sample)
                edit.setToolTip(tip + "\nExample: " + sample)
                self.edits[key] = edit
                row = QtWidgets.QHBoxLayout()
                row.addWidget(edit)
                browse = QtWidgets.QPushButton("Browse…")
                browse.clicked.connect(
                    lambda _checked=False, e=edit, k=kind: self._browse(e, k))
                row.addWidget(browse)
                form.addRow(label, row)
                help_label = QtWidgets.QLabel(tip + "\nExample: " + sample)
                help_label.setWordWrap(True)
                form.addRow(help_label)
            fields.addWidget(group)
        scroll.setWidget(content)
        layout.addWidget(scroll)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self, edit, kind):
        start = edit.text().strip()
        if start:
            start = str(path_config.configured_path(
                {"path": start}, "path", self._config_path))
        if kind == "directory":
            chosen = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose folder", start)
        elif kind == "save":
            chosen, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "PAC output", start, "PAC archive (*.pac)")
        else:
            chosen, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Choose executable", start, "Executables (*.exe);;All files (*)")
        if chosen:
            edit.setText(chosen)

    def _save(self):
        try:
            path_config.save_config(
                {key: edit.text() for key, edit in self.edits.items()},
                self._config_path, self._keys)
        except (OSError, ValueError) as exc:
            QtWidgets.QMessageBox.warning(self, "Settings not saved", str(exc))
            return
        self.accept()
