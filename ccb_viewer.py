"""Read-only FF16 CCB browser (PySide6).

Usage: python ccb_viewer.py [file.ccb | directory]
"""

import sys
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

import ccb_decode


class ShapePreview(QtWidgets.QWidget):
    """Schematic dimensions only: no unverified rotation or bone transforms."""
    def __init__(self):
        super().__init__()
        self.shape = None
        self.setMinimumHeight(240)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

    def sizeHint(self):
        return QtCore.QSize(600, 250)

    def set_shape(self, shape):
        self.shape = shape
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.fillRect(self.rect(), self.palette().base())
        painter.setPen(self.palette().text().color())
        shape = self.shape
        if not shape or shape['status'] != 'inferred':
            reason = shape['reason'] if shape else 'Select a collision to inspect its candidate shape.'
            painter.drawText(self.rect().adjusted(12, 12, -12, -12),
                             QtCore.Qt.AlignCenter | QtCore.Qt.TextWordWrap, reason)
            return
        radius = shape['radius']
        half = shape.get('half_segment_length', 0.0)
        title = f"{shape['kind'].title()} (inferred) · radius {radius:.5g}"
        if shape['kind'] == 'capsule':
            title += f" · half-segment {half:.5g} · total height {shape['total_height']:.5g}"
        painter.drawText(QtCore.QRectF(12, 8, self.width() - 24, 36),
                         QtCore.Qt.AlignLeft | QtCore.Qt.TextWordWrap, title)
        painter.setPen(QtGui.QColor('#8f9ba8'))
        painter.drawText(QtCore.QRectF(12, 42, self.width() - 24, 40),
                         QtCore.Qt.AlignLeft | QtCore.Qt.TextWordWrap,
                         'Schematic at origin; capsule axis assumed Y. Joint position and rotation are not applied.')
        # Normalize before scaling so tiny/large valid float32 dimensions are safe.
        extent = radius + half
        ratio = radius / extent
        scale = min((self.height() - 110) / 2, (self.width() - 80) / 4)
        scale = max(1.0, scale)
        pixel_radius = ratio * scale
        center_y = 86 + (self.height() - 110) / 2
        for center_x, side in ((self.width() / 4, True), (self.width() * 3 / 4, False)):
            painter.setPen(QtGui.QPen(QtGui.QColor('#46515d'), 1, QtCore.Qt.DashLine))
            painter.drawLine(QtCore.QPointF(center_x - scale - 8, center_y),
                             QtCore.QPointF(center_x + scale + 8, center_y))
            painter.drawLine(QtCore.QPointF(center_x, center_y - scale - 8),
                             QtCore.QPointF(center_x, center_y + scale + 8))
            painter.setPen(QtGui.QPen(QtGui.QColor('#75c7e3'), 2))
            painter.setBrush(QtGui.QColor(75, 150, 180, 40))
            height = scale if side else pixel_radius
            rect = QtCore.QRectF(center_x - pixel_radius, center_y - height,
                                pixel_radius * 2, height * 2)
            painter.drawRoundedRect(rect, pixel_radius, pixel_radius)
            painter.setPen(self.palette().text().color())
            painter.drawText(QtCore.QRectF(center_x - 90, self.height() - 23, 180, 20),
                             QtCore.Qt.AlignCenter, 'Side (XY)' if side else 'End (XZ)')


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, start_path=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('FF16 Character Collision Viewer')
        self.resize(1500, 900)
        self._decoded = None
        self._current_file = None
        menu = self.menuBar().addMenu('&File')
        menu.addAction('Open File…', self.open_file_dialog, 'Ctrl+O')
        menu.addAction('Open Folder…', self.open_dir_dialog, 'Ctrl+Shift+O')
        self.export_action = menu.addAction('Export JSON…', self.export_dialog, 'Ctrl+E')
        self.export_action.setEnabled(False)
        menu.addAction('Reload', self.reload, 'F5')

        left = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(left)
        for text, callback in (('Open File…', self.open_file_dialog),
                               ('Open Folder…', self.open_dir_dialog)):
            button = QtWidgets.QPushButton(text)
            button.clicked.connect(callback)
            layout.addWidget(button)
        self.file_list = QtWidgets.QListWidget()
        self.file_list.currentItemChanged.connect(self._file_selected)
        layout.addWidget(self.file_list)
        self.summary = QtWidgets.QLabel('Open a .ccb file or folder to begin.')
        self.summary.setTextFormat(QtCore.Qt.PlainText)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        header_button = QtWidgets.QPushButton('Inspect file header / warnings')
        header_button.clicked.connect(self.inspect_header)
        layout.addWidget(header_button)

        center = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(center)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText('Filter by ID, name, joint, shape, type or collision ID…')
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        self.tabs = QtWidgets.QTabWidget()
        layout.addWidget(self.tabs)
        self.models = []
        self.proxies = []
        self.tables = []
        for label, columns in (
                ('Collisions', ['Index', 'ID', 'Name', 'Debug name', 'Joint', 'Shape (inferred)', 'Type',
                                'Data type', 'Collision ID', 'Name 0x28']),
                ('EID definitions', ['Index', 'EID ID', 'Joint'])):
            model = QtGui.QStandardItemModel(0, len(columns), self)
            model.setHorizontalHeaderLabels(columns)
            proxy = QtCore.QSortFilterProxyModel(self)
            proxy.setSourceModel(model)
            proxy.setFilterKeyColumn(-1)
            proxy.setFilterCaseSensitivity(QtCore.Qt.CaseInsensitive)
            table = QtWidgets.QTableView()
            table.setModel(proxy)
            table.setSortingEnabled(True)
            table.sortByColumn(0, QtCore.Qt.AscendingOrder)
            table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
            table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
            table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            table.setAlternatingRowColors(True)
            table.verticalHeader().hide()
            table.horizontalHeader().setStretchLastSection(True)
            table.selectionModel().currentRowChanged.connect(self._selection_changed)
            self.models.append(model)
            self.proxies.append(proxy)
            self.tables.append(table)
            self.tabs.addTab(table, label)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.shape_preview = ShapePreview()
        layout.addWidget(self.shape_preview)

        right = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(right)
        note = QtWidgets.QLabel('Read-only. Shape, dimension and transform meanings are inferred '
                               'from c1001, not engine-verified. Raw template fields are preserved.')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.inspector = QtWidgets.QTreeWidget()
        self.inspector.setHeaderLabels(['Field', 'Value'])
        self.inspector.setColumnWidth(0, 185)
        self.inspector.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.inspector.setContextMenuPolicy(QtCore.Qt.ActionsContextMenu)
        copy_action = QtGui.QAction('Copy selected fields', self.inspector)
        copy_action.setShortcut(QtGui.QKeySequence.Copy)
        copy_action.setShortcutContext(QtCore.Qt.WidgetWithChildrenShortcut)
        copy_action.triggered.connect(self._copy_fields)
        self.inspector.addAction(copy_action)
        layout.addWidget(self.inspector)

        split = QtWidgets.QSplitter()
        for widget in (left, center, right):
            split.addWidget(widget)
        split.setSizes([230, 800, 470])
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)
        self.statusBar().showMessage('Open a .ccb file or folder to begin.')
        if start_path:
            self.open_path(start_path)

    def open_file_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, 'Open character collision', str(self._current_file.parent) if self._current_file else '',
            'Character Collision Binary (*.ccb);;All files (*)')
        if path:
            self.open_path(path)

    def open_dir_dialog(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, 'Open CCB folder')
        if path:
            self.open_path(path)

    def open_path(self, path):
        path = Path(path)
        if path.is_dir():
            try:
                files = sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() == '.ccb')
            except OSError as exc:
                QtWidgets.QMessageBox.warning(self, 'Cannot open folder', str(exc))
                return
            self.file_list.clear()
            for file in files:
                self._add_file(file)
            if files:
                self.file_list.setCurrentRow(0)
            else:
                self.statusBar().showMessage(f'No .ccb files in {path}')
        else:
            for row in range(self.file_list.count()):
                item = self.file_list.item(row)
                if Path(item.data(QtCore.Qt.UserRole)) == path:
                    if item == self.file_list.currentItem():
                        self.load_file(path)
                    else:
                        self.file_list.setCurrentItem(item)
                    return
            item = self._add_file(path)
            self.file_list.setCurrentItem(item)

    def _add_file(self, path):
        item = QtWidgets.QListWidgetItem(path.name)
        item.setToolTip(str(path))
        item.setData(QtCore.Qt.UserRole, str(path))
        self.file_list.addItem(item)
        return item

    def _file_selected(self, current, previous):
        if current:
            self.load_file(current.data(QtCore.Qt.UserRole))

    def load_file(self, path):
        try:
            decoded = ccb_decode.decode(path)
        except (OSError, ValueError) as exc:
            QtWidgets.QMessageBox.warning(self, 'Cannot decode CCB', str(exc))
            return False
        self._decoded = decoded
        self._current_file = Path(path)
        self.setWindowTitle(f'{self._current_file.name} — FF16 Character Collision Viewer')
        self.inspector.clear()
        for model in self.models:
            model.removeRows(0, model.rowCount())
        for entry in decoded['entries']:
            collision = entry['data_0x54']
            self._append_row(0, entry, [entry['index'], entry['id'], entry['name'],
                entry['debug_name'], entry['joint_name'], entry['shape'].get('kind', 'unknown'), entry['unk_type'],
                entry['data_0x20_type'], collision['collision_id'] if collision else '', entry['name_0x28']])
        for eid in decoded['eid_definitions']:
            self._append_row(1, eid, [eid['index'], eid['eid_id'], eid['joint_name']])
        for table in self.tables:
            table.resizeColumnsToContents()
        self.summary.setText(f"{decoded['name']}\nVersion {decoded['version']} · {decoded['size']:,} bytes\n"
                             f"{decoded['entry_count']:,} collisions\n"
                             f"{decoded['eid_def_count']:,} EID definitions\n"
                             f"{len(decoded['warnings'])} warnings")
        self.export_action.setEnabled(True)
        self._filter(self.search.text())
        return True

    def _append_row(self, table_index, record, values):
        items = []
        for value in values:
            item = QtGui.QStandardItem()
            item.setData(value, QtCore.Qt.DisplayRole)
            items.append(item)
        # Store the source index, so sorting/filtering cannot change identity.
        items[0].setData(record['index'], QtCore.Qt.UserRole)
        self.models[table_index].appendRow(items)

    def _filter(self, text):
        for proxy in self.proxies:
            proxy.setFilterFixedString(text)
        self._tab_changed(self.tabs.currentIndex())

    def _tab_changed(self, index):
        table = self.tables[index]
        if self.proxies[index].rowCount():
            table.selectRow(0)
            self._selection_changed(table.currentIndex(), QtCore.QModelIndex())
        else:
            self.inspect_header()
        self._update_status()

    def _update_status(self):
        if self._decoded:
            index = self.tabs.currentIndex()
            self.statusBar().showMessage(
                f'{self._current_file} · {self.proxies[index].rowCount():,} / '
                f'{self.models[index].rowCount():,} rows · read-only')

    def _selection_changed(self, current, previous):
        if not self._decoded or not current.isValid():
            return
        index = self.tabs.currentIndex()
        if current.model() is not self.proxies[index]:
            return
        source = self.proxies[index].mapToSource(current)
        row = self.models[index].item(source.row(), 0).data(QtCore.Qt.UserRole)
        records = self._decoded['entries' if index == 0 else 'eid_definitions']
        self._inspect(records[row])

    def inspect_header(self):
        if self._decoded:
            self._inspect({key: value for key, value in self._decoded.items()
                           if key not in ('entries', 'eid_definitions')})

    def _inspect(self, record):
        self.inspector.clear()
        self.shape_preview.set_shape(record.get('shape'))

        def add(parent, key, value):
            container = isinstance(value, (dict, list))
            text = f'{len(value)} items' if container else ('—' if value is None else str(value))
            if isinstance(value, int) and ('offset' in key or key == 'offset'):
                text = f'0x{value:X} ({value})'
            item = QtWidgets.QTreeWidgetItem(parent, [str(key), text])
            item.setToolTip(1, text)
            if container:
                for child_key, child_value in (value.items() if isinstance(value, dict) else enumerate(value)):
                    add(item, str(child_key), child_value)
                item.setExpanded(key != 'float_interpretation')

        # Put inferred geometry first while retaining all original template fields.
        ordered = ({'shape': record['shape'], **record} if 'shape' in record else record)
        for key, value in ordered.items():
            add(self.inspector, key, value)

    def _copy_fields(self):
        QtWidgets.QApplication.clipboard().setText('\n'.join(
            f'{item.text(0)}: {item.text(1)}' for item in self.inspector.selectedItems()))

    def reload(self):
        if self._current_file:
            self.load_file(self._current_file)

    def export_dialog(self):
        if not self._decoded:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, 'Export decoded CCB', str(self._current_file.with_suffix('.json')), 'JSON (*.json)')
        if path:
            try:
                ccb_decode.export_json(self._decoded, path)
            except (OSError, ValueError) as exc:
                QtWidgets.QMessageBox.warning(self, 'Export failed', str(exc))
                return
            self.statusBar().showMessage(f'Exported {path}')


def main(argv=None):
    argv = sys.argv if argv is None else argv
    app = QtWidgets.QApplication(argv)
    from vfxb_viewer import apply_dark_theme
    apply_dark_theme(app)
    window = MainWindow(argv[1] if len(argv) > 1 else None)
    window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
