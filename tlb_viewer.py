"""
FF16 .tlb timeline visualizer + editor (PySide6).

  Left   : file list (open a single file or a folder) · Save / Restore
  Center : horizontal timeline — one row per element, colored by type group
           pan (drag), zoom (wheel), click a bar to inspect
  Right  : inspector / editor — edit scalar fields, delete the element

Editing:
  * Click a bar, change any scalar field, hit "Apply changes".
  * "Delete element" removes it from the timeline (dead-slot shift).
  * Edits live in memory until you Save (Ctrl+S). On the first save the
    untouched original is copied to <name>_origin.tlb and the edited file
    keeps its original name. "Restore original" copies the backup back.
  Scalar fields only (ints / bools / floats) are editable in place; strings,
  asset paths, and array layouts are shown read-only — see tlb_encode.py.

Run:
    python tlb_viewer.py [file.tlb | directory]
"""

import sys
import shutil
import subprocess
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

import tlb_decode
import tlb_encode

# ─────────────────────────────────────────────── repack config
# On save, the edited .tlb is also mirrored into a PackBack tree (preserving
# its path relative to the raw extracted-data root) and FF16Tools.CLI packs
# that tree into a .diff.pac mod archive.
FF16TOOLS_CLI = Path(
    r"D:\Softwares\FF16Tools.CLI-win-x64\win-x64\FF16Tools.CLI.exe")
RAW_ROOT = Path(r"\\ds423plus\devdata\FF16Raw")
PACKBACK_DIR = Path(r"\\ds423plus\devdata\FF16Raw_Pack\Timelines")
DIFF_PAC = Path(
    r"D:\SteamLibrary\steamapps\common\FINAL FANTASY XVI\data\0001.diff.pac")

# ─────────────────────────────────────────────── layout constants
RULER_H    = 28       # top ruler strip height
ROW_H      = 22       # one timeline row
ROW_GAP    = 3        # gap between rows
LABEL_W    = 220      # left label column (scene-space, unzoomed)
PX_FRAME   = 10.0     # initial pixels per frame
MIN_PX     = 1.5
MAX_PX     = 60.0
PAD        = 5

MAX_RECENT = 12

# ─────────────────────────────────────────────── type-group colours
_TYPE_COLOR = {
    # animation
    1001: "#2d6b45", 1035: "#2d6b45", 1002: "#357a50",
    # sound
    31: "#3b5b78",   45: "#2e5068",   57: "#2e5068", 56: "#2e5068",
    # VFX / FX triggers
    1023: "#8a6d1f", 1030: "#8a6d1f", 1049: "#7a5e18",
    # control / permissions
    17: "#6b2e6b",   74: "#6b2e6b",   1009: "#6b2e6b",
    1014: "#5a2060", 1130: "#5a2060",
    # battle / combat
    10: "#8a2e2e",   12: "#7a2828",   84: "#7a2828",
    9: "#7a3030",
    # camera
    8: "#1f6b8a",    1099: "#1f6b8a",
    # movement / force
    27: "#8a5a1f",   1010: "#8a5a1f", 1086: "#8a5a1f", 1088: "#8a5a1f",
    1007: "#8a6030",
    # voice / message
    1053: "#6b1f8a", 47: "#7a2e9a",
    # linked element (cross-references another element in the same file)
    37: "#4a4a1f",
    # summon
    1011: "#4a6b2e", 1012: "#4a6b2e", 1047: "#3a5a25",
    # combo / input
    1009: "#5a3a6b", 1016: "#5a3a6b",
    # magic burst
    1084: "#6b4a1f",
    # sound / SE
    1056: "#2e5068",
    # disable/enable misc
    51: "#4a2e1f",   1058: "#4a2e1f", 1097: "#4a2e1f",
}
_DEFAULT_COLOR = "#4a4a54"


def _elem_color(union_type):
    return QtGui.QColor(_TYPE_COLOR.get(union_type, _DEFAULT_COLOR))


# ─────────────────────────────────────────────── scene items

class BarItem(QtWidgets.QGraphicsItem):
    """One element row: label on the left, coloured bar for frame range."""

    def __init__(self, elem, row_idx, px_per_frame, on_click):
        super().__init__()
        self.elem        = elem
        self.row_idx     = row_idx
        self.px_per_frame = px_per_frame
        self.on_click    = on_click
        self._hover      = False
        self._selected   = False

        self.y0     = RULER_H + row_idx * (ROW_H + ROW_GAP)
        self.bar_x  = LABEL_W + elem["frame_start"] * px_per_frame
        raw_w = elem["num_frames"] * px_per_frame
        self.bar_w  = max(raw_w, 3.0)      # one-shot (0 frames) → thin tick
        self.is_oneshot = (elem["num_frames"] == 0)
        self.color  = _elem_color(elem["union_type"])

        self.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.setZValue(1)

    def boundingRect(self):
        # covers both the label region and the bar
        return QtCore.QRectF(0, self.y0, LABEL_W + self.bar_x + self.bar_w + 4,
                             ROW_H)

    # narrow bounding box used for click-testing only the interactive area
    def _active_rect(self):
        return QtCore.QRectF(self.bar_x - 2, self.y0, self.bar_w + 4, ROW_H)

    def paint(self, p, option, widget=None):
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        elem = self.elem
        y    = self.y0

        # ── label column ──────────────────────────────────────────────────
        label_rect = QtCore.QRectF(0, y, LABEL_W - PAD, ROW_H)
        type_name  = elem["type_name"]
        name       = elem.get("name") or ""
        display    = f"{type_name}"
        if name:
            display = f"{name}  [{type_name}]"

        p.setPen(QtGui.QColor("#9a9aa5"))
        f = p.font(); f.setPointSize(7); f.setBold(False); p.setFont(f)
        metrics = p.fontMetrics()
        display = metrics.elidedText(display, QtCore.Qt.ElideRight,
                                     int(LABEL_W - PAD * 2))
        p.drawText(label_rect, QtCore.Qt.AlignVCenter | QtCore.Qt.AlignRight,
                   display)

        # ── bar ───────────────────────────────────────────────────────────
        c = self.color
        bar = QtCore.QRectF(self.bar_x, y + 2, self.bar_w, ROW_H - 4)

        if self.is_oneshot:
            # thin vertical tick
            p.setBrush(c)
            p.setPen(QtCore.Qt.NoPen)
            p.drawRoundedRect(
                QtCore.QRectF(self.bar_x - 1.5, y + 1, 3, ROW_H - 2), 1, 1)
        else:
            fill = c.lighter(120) if (self._hover or self._selected) else c
            p.setBrush(fill)
            border = QtGui.QColor("#ffffff") if self._selected else c.lighter(150)
            p.setPen(QtGui.QPen(border, 1.2 if self._selected else 0.8))
            p.drawRoundedRect(bar, 3, 3)

            # bar label: frame range + type_name
            p.setPen(QtGui.QColor("#e8e8ee"))
            fb = p.font(); fb.setPointSize(7); fb.setBold(True); p.setFont(fb)
            bar_text = (f"{elem['frame_start']}–"
                        f"{elem['frame_start'] + elem['num_frames']}  {type_name}")
            bar_text = p.fontMetrics().elidedText(
                bar_text, QtCore.Qt.ElideRight, int(self.bar_w - PAD * 2))
            p.drawText(bar.adjusted(PAD, 0, -PAD, 0),
                       QtCore.Qt.AlignVCenter, bar_text)

    def mousePressEvent(self, event):
        self.on_click(self)
        super().mousePressEvent(event)

    def hoverEnterEvent(self, event):
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)


class RulerItem(QtWidgets.QGraphicsItem):
    """Top ruler showing frame numbers."""

    def __init__(self, total_frames, px_per_frame):
        super().__init__()
        self.total_frames = total_frames
        self.px_per_frame = px_per_frame
        self.setZValue(2)

    def boundingRect(self):
        return QtCore.QRectF(0, 0,
                             LABEL_W + self.total_frames * self.px_per_frame + 60,
                             RULER_H)

    def paint(self, p, option, widget=None):
        p.fillRect(self.boundingRect(), QtGui.QColor("#1a1a1f"))
        p.setPen(QtGui.QPen(QtGui.QColor("#3a3a44"), 1))
        p.drawLine(QtCore.QPointF(0, RULER_H - 1),
                   QtCore.QPointF(self.boundingRect().width(), RULER_H - 1))

        px = self.px_per_frame
        # choose a tick interval that gives >= 40 px between ticks
        for interval in (1, 2, 5, 10, 20, 30, 50, 100):
            if interval * px >= 40:
                break

        f = p.font(); f.setPointSize(7); p.setFont(f)
        frame = 0
        while frame <= self.total_frames + interval:
            x = LABEL_W + frame * px
            p.setPen(QtGui.QPen(QtGui.QColor("#5a5a64"), 1))
            p.drawLine(QtCore.QPointF(x, RULER_H - 8),
                       QtCore.QPointF(x, RULER_H - 1))
            p.setPen(QtGui.QColor("#9a9aa5"))
            p.drawText(QtCore.QRectF(x - 20, 4, 40, 16),
                       QtCore.Qt.AlignHCenter, str(frame))
            frame += interval


class TimelineView(QtWidgets.QGraphicsView):
    """Pan-with-drag, zoom-with-wheel timeline canvas."""

    def __init__(self):
        super().__init__()
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setRenderHint(QtGui.QPainter.Antialiasing)
        self.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(QtGui.QColor("#1e1e24"))
        self._zoom = 1.0
        self._px_per_frame = PX_FRAME
        self._bars = []
        self._selected_bar = None
        self._on_select = None
        self._total_frames = 0

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        new_zoom = self._zoom * factor
        new_px = self._px_per_frame * factor
        if MIN_PX <= new_px <= MAX_PX:
            self._zoom = new_zoom
            self._px_per_frame = new_px
            self.scale(factor, factor)

    def build(self, elements, total_frames, on_select):
        self._on_select = on_select
        self._total_frames = total_frames
        self._bars = []
        self._selected_bar = None
        scene = self.scene()
        scene.clear()

        # frame-grid vertical lines
        grid_pen = QtGui.QPen(QtGui.QColor("#2a2a32"), 1)
        grid_pen.setCosmetic(True)
        for f in range(0, total_frames + 1, 1):
            x = LABEL_W + f * self._px_per_frame
            scene.addLine(x, RULER_H, x,
                          RULER_H + len(elements) * (ROW_H + ROW_GAP),
                          grid_pen)

        # alternating row backgrounds
        for i in range(len(elements)):
            y = RULER_H + i * (ROW_H + ROW_GAP)
            bg = QtGui.QColor("#222228" if i % 2 == 0 else "#1e1e24")
            r = scene.addRect(0, y,
                              LABEL_W + total_frames * self._px_per_frame + 60,
                              ROW_H, QtGui.QPen(QtCore.Qt.NoPen),
                              QtGui.QBrush(bg))
            r.setZValue(0)

        # label divider line
        div_pen = QtGui.QPen(QtGui.QColor("#3a3a44"), 1)
        scene.addLine(LABEL_W - 1, 0, LABEL_W - 1,
                      RULER_H + len(elements) * (ROW_H + ROW_GAP), div_pen)

        # ruler
        ruler = RulerItem(total_frames, self._px_per_frame)
        scene.addItem(ruler)

        # element bars
        for i, elem in enumerate(elements):
            bar = BarItem(elem, i, self._px_per_frame, self._on_bar_click)
            scene.addItem(bar)
            self._bars.append(bar)

        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-20, -20, 60, 40))
        self.resetTransform()
        self._zoom = 1.0
        self.centerOn(LABEL_W, RULER_H + len(elements) * (ROW_H + ROW_GAP) / 2)

    def _on_bar_click(self, bar):
        if self._selected_bar and self._selected_bar is not bar:
            self._selected_bar._selected = False
            self._selected_bar.update()
        bar._selected = True
        self._selected_bar = bar
        bar.update()
        if self._on_select:
            self._on_select(bar.elem)


# ─────────────────────────────────────────────── inspector

class Inspector(QtWidgets.QScrollArea):
    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self._host = QtWidgets.QWidget()
        self._lay  = QtWidgets.QVBoxLayout(self._host)
        self._lay.setAlignment(QtCore.Qt.AlignTop)
        self.setWidget(self._host)

        # editing state / callbacks (wired by MainWindow)
        self._apply_cb  = None   # (list[(offset, fmt, value)]) -> None
        self._delete_cb = None   # (elem_index) -> None
        self._editors   = []     # list of (offset, fmt, QLineEdit)
        self._cur_elem  = None

        self._show_placeholder()

    def set_callbacks(self, apply_cb, delete_cb):
        self._apply_cb  = apply_cb
        self._delete_cb = delete_cb

    def _clear(self):
        # recursively remove widgets AND nested sub-layouts. The editor adds
        # QHBoxLayout/QFormLayout via addLayout(); those items have no
        # .widget(), so a top-level-only sweep would orphan their children
        # (they stay parented to the host and keep painting over new content).
        self._editors = []
        self._clear_layout(self._lay)

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            else:
                child = item.layout()
                if child is not None:
                    self._clear_layout(child)
                    child.deleteLater()

    def _show_placeholder(self):
        self._clear()
        lbl = QtWidgets.QLabel("Click a timeline bar to inspect it.")
        lbl.setStyleSheet("color:#888; padding:12px;")
        self._lay.addWidget(lbl)

    def _heading(self, text, size=11, bold=True, color="#ffffff"):
        lbl = QtWidgets.QLabel(text)
        f = lbl.font(); f.setPointSize(size); f.setBold(bold); lbl.setFont(f)
        lbl.setStyleSheet(f"color:{color};")
        lbl.setWordWrap(True)
        return lbl

    def _mono(self, text, color="#cfe8ff"):
        lbl = QtWidgets.QLabel(text)
        lbl.setStyleSheet(f"color:{color}; font-family:Consolas; font-size:8pt;")
        lbl.setWordWrap(True)
        return lbl

    def _divider(self):
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setStyleSheet("color:#3a3a44;")
        return line

    def show_element(self, elem, sections=None):
        self._clear()
        self._editors = []
        self._cur_elem = elem

        type_color = _TYPE_COLOR.get(elem["union_type"], _DEFAULT_COLOR)
        self._lay.addWidget(
            self._heading(f'{elem["type_name"]}  (type {elem["union_type"]})',
                          size=11, bold=True,
                          color=QtGui.QColor(type_color).lighter(180).name()))

        # Frame range
        fs = elem["frame_start"]
        nf = elem["num_frames"]
        kind = "OneShot" if nf == 0 else "Range"
        frame_str = (f"frame {fs}" if nf == 0
                     else f"frames {fs} – {fs + nf}  ({nf} frames)")
        self._lay.addWidget(self._heading(f"{kind}  ·  {frame_str}",
                                          size=8, bold=False, color="#b9b9c0"))

        if elem.get("name"):
            self._lay.addWidget(self._heading(f'name: "{elem["name"]}"',
                                              size=8, bold=False, color="#b9b9c0"))

        # ── action row: delete this element ──────────────────────────────
        act_row = QtWidgets.QHBoxLayout()
        btn_del = QtWidgets.QPushButton("🗑  Delete element")
        btn_del.setStyleSheet(
            "QPushButton{background:#5a2020;color:#ffd0d0;padding:4px;}"
            "QPushButton:hover{background:#7a2828;}")
        btn_del.clicked.connect(self._on_delete_clicked)
        act_row.addWidget(btn_del)
        act_row.addStretch(1)
        self._lay.addLayout(act_row)

        self._lay.addWidget(self._divider())

        # ── editable fields (built from tlb_encode.build_field_map) ──────
        if sections:
            for sec in sections:
                self._lay.addWidget(self._heading(sec["title"], size=9))
                # wrap each form in its own widget so the vertical layout
                # reserves the correct height (a bare nested QFormLayout under
                # an AlignTop QVBoxLayout collapses and rows overlap)
                form_host = QtWidgets.QWidget()
                form = QtWidgets.QFormLayout(form_host)
                form.setContentsMargins(6, 0, 6, 6)
                form.setSpacing(3)
                form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                form.setFieldGrowthPolicy(
                    QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
                for fld in sec["fields"]:
                    edit = self._make_editor(fld)
                    lbl = QtWidgets.QLabel(f'{fld["key"]}  <{fld["fmt"]}>')
                    lbl.setStyleSheet("color:#9a9aa5; font-family:Consolas;"
                                      " font-size:8pt;")
                    form.addRow(lbl, edit)
                self._lay.addWidget(form_host)

            # Apply / Revert buttons
            btn_row = QtWidgets.QHBoxLayout()
            btn_apply = QtWidgets.QPushButton("Apply changes")
            btn_apply.setStyleSheet(
                "QPushButton{background:#204a5a;color:#d0f0ff;padding:5px;"
                "font-weight:bold;}QPushButton:hover{background:#286078;}")
            btn_apply.clicked.connect(self._on_apply_clicked)
            btn_revert = QtWidgets.QPushButton("Revert fields")
            btn_revert.clicked.connect(self._on_revert_clicked)
            btn_row.addWidget(btn_apply)
            btn_row.addWidget(btn_revert)
            self._lay.addLayout(btn_row)

        # ── read-only decoded payload (strings, paths, hex, arrays) ──────
        data = elem.get("data")
        if data:
            self._lay.addWidget(self._divider())
            self._lay.addWidget(self._heading("Decoded payload (read-only)",
                                              size=9))
            if "decode_error" in data:
                self._lay.addWidget(
                    self._mono(f"  [error] {data['decode_error']}", "#ff8888"))
            self._add_dict(data)

    # ── field editors ─────────────────────────────────────────────────────

    def _make_editor(self, fld):
        """Build a QLineEdit for one scalar field and remember it for Apply."""
        edit = QtWidgets.QLineEdit()
        edit.setStyleSheet("font-family:Consolas; font-size:8pt;")
        val = fld["value"]
        edit.setText(f"{val:.6g}" if isinstance(val, float) else str(val))
        edit.setProperty("orig_text", edit.text())
        self._editors.append((fld["offset"], fld["fmt"], edit))
        return edit

    def _on_apply_clicked(self):
        if not self._apply_cb:
            return
        patches = []
        for offset, fmt, edit in self._editors:
            text = edit.text()
            if text == edit.property("orig_text"):
                continue                      # unchanged — skip
            try:
                value = tlb_encode.parse_value(fmt, text)
            except ValueError as e:
                QtWidgets.QMessageBox.warning(
                    self, "Invalid value",
                    f"Field <{fmt}>: {e}\nText was: {text!r}")
                return
            patches.append((offset, fmt, value))
        if not patches:
            return
        self._apply_cb(patches)

    def _on_revert_clicked(self):
        for _off, _fmt, edit in self._editors:
            edit.setText(edit.property("orig_text"))

    def _on_delete_clicked(self):
        if not self._delete_cb or self._cur_elem is None:
            return
        idx = self._cur_elem["index"]
        name = self._cur_elem.get("name") or self._cur_elem["type_name"]
        resp = QtWidgets.QMessageBox.question(
            self, "Delete element",
            f"Delete timeline element #{idx}\n({name})?\n\n"
            "This removes it from the timeline. The original file is "
            "preserved as *_origin.tlb until you save.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if resp == QtWidgets.QMessageBox.Yes:
            self._delete_cb(idx)

    def _add_dict(self, d, indent=1):
        prefix = "  " * indent
        for k, v in d.items():
            if k == "decode_error":
                continue
            if k == "vfx_emit_params" and isinstance(v, list):
                self._add_vfx_emit_params(v, indent)
            elif k == "vfx_external_params" and isinstance(v, list):
                self._add_vfx_external_params(v, indent)
            elif isinstance(v, dict):
                self._lay.addWidget(
                    self._heading(f"{prefix}{k}:", size=8, bold=True,
                                  color="#d0d0d8"))
                self._add_dict(v, indent + 1)
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                self._lay.addWidget(
                    self._heading(f"{prefix}{k}:", size=8, bold=True,
                                  color="#d0d0d8"))
                for i, item in enumerate(v):
                    self._lay.addWidget(
                        self._heading(f"{prefix}  [{i}]", size=8, bold=False,
                                      color="#9a9aa5"))
                    self._add_dict(item, indent + 2)
            elif isinstance(v, list):
                self._lay.addWidget(
                    self._mono(f"{prefix}{k} = [{', '.join(str(x) for x in v)}]"))
            elif isinstance(v, float):
                self._lay.addWidget(self._mono(f"{prefix}{k} = {v:.6g}"))
            else:
                color = "#ffcf70" if "path" in k or k == "raw" else "#cfe8ff"
                self._lay.addWidget(self._mono(f"{prefix}{k} = {v}", color))

    def _add_vfx_emit_params(self, params, indent):
        prefix = "  " * indent
        self._lay.addWidget(
            self._heading(f"{prefix}vfx_emit_params  "
                          f"({len(params)} instance{'s' if len(params) != 1 else ''})",
                          size=8, bold=True, color="#ffcf70"))
        for i, p in enumerate(params):
            active = p.get("active", True)
            dim = "" if active else " (inactive)"
            self._lay.addWidget(self._heading(
                f"{prefix}  [{i}]{dim}", size=8, bold=False,
                color="#e8e8ee" if active else "#7a7a82"))

            attach = p.get("attach_eid", 0)
            eid1, eid2 = p.get("eid_id1", 0), p.get("eid_id2", 0)
            attach_src = "eid_id2" if eid2 else ("eid_id1" if eid1 else "none")
            self._lay.addWidget(self._mono(
                f"{prefix}    attach eid = {attach}  "
                f"(from {attach_src}; id1={eid1}, id2={eid2})",
                "#cfe8ff" if attach else "#7a7a82"))

            off = p.get("offset", {})
            self._lay.addWidget(self._mono(
                f"{prefix}    local offset  X={off.get('x', 0):.4f}  "
                f"Y={off.get('y', 0):.4f}  Z={off.get('z', 0):.4f}"))
            self._lay.addWidget(self._mono(
                f"{prefix}    revolution={p.get('revolution', 0):.4f}  "
                f"rotation={p.get('rotation', 0):.4f}  "
                f"scale={p.get('scale', 1):.4f}"))
            self._lay.addWidget(self._mono(
                f"{prefix}    unk_id_slot={p.get('unk_id_slot', 0)}",
                "#9a9aa5"))

    def _add_vfx_external_params(self, params, indent):
        # per-spawn override list for named parameters inside the .vfxb
        # graph (analogous to Niagara User Parameters) - param_id is a
        # hash/index into that graph's constants/curves, not resolved here.
        prefix = "  " * indent
        self._lay.addWidget(
            self._heading(f"{prefix}vfx_external_params  "
                          f"({len(params)} override{'s' if len(params) != 1 else ''})",
                          size=8, bold=True, color="#ffcf70"))
        for p in params:
            self._lay.addWidget(self._mono(
                f"{prefix}    param_id={p.get('param_id', 0)}  "
                f"value={p.get('value', 0):.4g}  "
                f"kind={p.get('kind', 0)}  unk_0x08={p.get('unk_0x08', 0)}"))


# ─────────────────────────────────────────────── main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, start_path=None):
        super().__init__()
        self.setWindowTitle("FF16 Timeline Viewer")
        self.resize(1600, 900)
        self._current_dir = None
        self._current_file = None
        self._buf = None        # mutable bytearray of the file being edited
        self._dirty = False     # unsaved in-memory edits

        self._settings = QtCore.QSettings("FF16Tools", "TlbViewer")

        # ── menu bar ──────────────────────────────────────────────────────
        menu = self.menuBar().addMenu("&File")
        act_open_file = menu.addAction("Open File…")
        act_open_file.triggered.connect(self._open_file_dialog)
        act_open_dir = menu.addAction("Open Folder…")
        act_open_dir.triggered.connect(self._open_dir_dialog)
        menu.addSeparator()
        self._act_save = menu.addAction("Save")
        self._act_save.setShortcut("Ctrl+S")
        self._act_save.triggered.connect(self._save_current)
        self._act_restore = menu.addAction("Restore original (*_origin.tlb)")
        self._act_restore.triggered.connect(self._restore_current)
        menu.addSeparator()
        self._recent_menu = menu.addMenu("Recent")
        menu.addSeparator()
        act_clear_recent = menu.addAction("Clear Recent")
        act_clear_recent.triggered.connect(self._clear_recent)
        self._rebuild_recent_menu()

        # ── left panel ────────────────────────────────────────────────────
        left = QtWidgets.QWidget()
        lv   = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(6, 6, 6, 6)

        btn_row = QtWidgets.QHBoxLayout()
        b_file = QtWidgets.QPushButton("Open File…")
        b_dir  = QtWidgets.QPushButton("Open Folder…")
        b_file.clicked.connect(self._open_file_dialog)
        b_dir.clicked.connect(self._open_dir_dialog)
        btn_row.addWidget(b_file)
        btn_row.addWidget(b_dir)
        lv.addLayout(btn_row)

        self.file_list = QtWidgets.QListWidget()
        self.file_list.currentItemChanged.connect(self._file_selected)
        lv.addWidget(self.file_list)

        self.b_add = QtWidgets.QPushButton("Add element…")
        self.b_add.setStyleSheet(
            "QPushButton{background:#2a4a2a;color:#d0ffd0;}"
            "QPushButton:hover{background:#356035;}")
        self.b_add.clicked.connect(self._add_element_dialog)
        self.b_add.setEnabled(False)
        lv.addWidget(self.b_add)

        save_row = QtWidgets.QHBoxLayout()
        self.b_save = QtWidgets.QPushButton("Save")
        self.b_save.setStyleSheet(
            "QPushButton{background:#204a5a;color:#d0f0ff;font-weight:bold;}"
            "QPushButton:hover{background:#286078;}")
        self.b_save.clicked.connect(self._save_current)
        self.b_restore = QtWidgets.QPushButton("Restore original")
        self.b_restore.clicked.connect(self._restore_current)
        save_row.addWidget(self.b_save)
        save_row.addWidget(self.b_restore)
        lv.addLayout(save_row)

        self.summary = QtWidgets.QLabel("")
        self.summary.setStyleSheet("color:#b9b9c0; padding:4px;")
        self.summary.setWordWrap(True)
        lv.addWidget(self.summary)

        # ── center: timeline ──────────────────────────────────────────────
        self.timeline = TimelineView()

        # ── right: inspector ──────────────────────────────────────────────
        self.inspector = Inspector()
        self.inspector.set_callbacks(self._apply_patches, self._delete_element)

        split = QtWidgets.QSplitter()
        split.addWidget(left)
        split.addWidget(self.timeline)
        split.addWidget(self.inspector)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([240, 1000, 360])
        self.setCentralWidget(split)

        self.statusBar().showMessage("Open a .tlb file or a folder to begin.")

        if start_path:
            p = Path(start_path)
            if p.is_dir():
                self._load_dir(p)
            elif p.is_file():
                self._load_dir(p.parent, select=p.name)
        else:
            self._restore_last_session()

    # ── recent files/folders ─────────────────────────────────────────────

    def _recent_list(self):
        return self._settings.value("recent", [], type=list)

    def _add_recent(self, path):
        entry = str(path)
        recent = [r for r in self._recent_list() if r != entry]
        recent.insert(0, entry)
        recent = recent[:MAX_RECENT]
        self._settings.setValue("recent", recent)
        self._rebuild_recent_menu()

    def _clear_recent(self):
        self._settings.setValue("recent", [])
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self):
        self._recent_menu.clear()
        recent = self._recent_list()
        if not recent:
            act = self._recent_menu.addAction("(empty)")
            act.setEnabled(False)
            return
        for entry in recent:
            p = Path(entry)
            label = str(p) if p.is_dir() else f"{p.name}   ({p.parent})"
            act = self._recent_menu.addAction(label)
            act.triggered.connect(
                lambda _checked=False, e=entry: self._open_recent(e))

    def _open_recent(self, entry):
        p = Path(entry)
        if p.is_dir():
            self._load_dir(p)
        elif p.is_file():
            self._load_dir(p.parent, select=p.name)
        else:
            self.statusBar().showMessage(f"No longer exists: {entry}")

    def _restore_last_session(self):
        last_file = self._settings.value("last_file", "", type=str)
        last_dir = self._settings.value("last_dir", "", type=str)
        if last_file and Path(last_file).is_file():
            p = Path(last_file)
            self._load_dir(p.parent, select=p.name)
        elif last_dir and Path(last_dir).is_dir():
            self._load_dir(Path(last_dir))

    # ── file handling ──────────────────────────────────────────────────────

    def _open_file_dialog(self):
        start = self._current_dir or self._settings.value(
            "last_browse_dir", "", type=str)
        fn, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open TLB", str(start),
            "Timeline Binary (*.tlb);;All files (*)")
        if fn:
            p = Path(fn)
            self._settings.setValue("last_browse_dir", str(p.parent))
            self._load_dir(p.parent, select=p.name)

    def _open_dir_dialog(self):
        start = self._current_dir or self._settings.value(
            "last_browse_dir", "", type=str)
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Open folder", str(start))
        if d:
            self._settings.setValue("last_browse_dir", d)
            self._load_dir(Path(d))

    def _load_dir(self, directory, select=None):
        self._current_dir = directory
        self.file_list.blockSignals(True)
        self.file_list.clear()
        files = sorted(directory.glob("*.tlb"))
        for f in files:
            self.file_list.addItem(f.name)
        self.file_list.blockSignals(False)

        self._settings.setValue("last_dir", str(directory))
        self._add_recent(directory)

        if not files:
            self.statusBar().showMessage(f"No .tlb files in {directory}")
            return
        names = [f.name for f in files]
        target = select if select in names else files[0].name
        items = self.file_list.findItems(target, QtCore.Qt.MatchExactly)
        if items:
            self.file_list.setCurrentItem(items[0])

    def _file_selected(self, cur, _prev):
        if cur is None or self._current_dir is None:
            return
        if not self._confirm_discard():
            # revert selection back to the current file without reloading
            self.file_list.blockSignals(True)
            items = self.file_list.findItems(
                self._current_file.name if self._current_file else "",
                QtCore.Qt.MatchExactly)
            if items:
                self.file_list.setCurrentItem(items[0])
            self.file_list.blockSignals(False)
            return
        self._load_file(self._current_dir / cur.text())

    def _confirm_discard(self):
        """If there are unsaved edits, ask. Return True to proceed."""
        if not self._dirty:
            return True
        resp = QtWidgets.QMessageBox.question(
            self, "Unsaved changes",
            "This file has unsaved edits. Discard them?",
            QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel)
        return resp == QtWidgets.QMessageBox.Discard

    def _load_file(self, path):
        self._current_file = path
        self._settings.setValue("last_file", str(path))
        self._add_recent(path)
        try:
            with open(path, "rb") as fh:
                self._buf = bytearray(fh.read())
        except Exception as e:
            self.statusBar().showMessage(f"Read failed: {e}")
            QtWidgets.QMessageBox.warning(self, "Read failed", str(e))
            self._buf = None
            return
        self._dirty = False
        self._render_from_buf()

    def _render_from_buf(self):
        """(Re)decode the in-memory buffer and rebuild timeline + summary."""
        path = self._current_file
        try:
            decoded = tlb_decode.decode_tlb(bytes(self._buf))
        except Exception as e:
            self.statusBar().showMessage(f"Decode failed: {e}")
            QtWidgets.QMessageBox.warning(self, "Decode failed", str(e))
            return

        elements = decoded["timeline_elements"]
        total_frames = decoded["total_frames"]

        self.timeline.build(elements, total_frames, self._on_elem_selected)
        self.inspector._show_placeholder()

        n_assets = sum(len(ag["assets"]) for ag in decoded["asset_groups"])
        dirty_mark = "  •unsaved" if self._dirty else ""
        has_bak = tlb_encode.has_backup(path)
        self.summary.setText(
            f"<b>{path.name}</b>{dirty_mark}<br>"
            f"elements: {len(elements)}  ·  frames: {total_frames}<br>"
            f"asset groups: {decoded['asset_group_count']}  "
            f"({n_assets} assets)<br>"
            f"targets: {decoded['target_count']}"
            + ("<br><span style='color:#7a9'>backup: *_origin.tlb exists</span>"
               if has_bak else "")
        )
        self.b_restore.setEnabled(has_bak)
        self._act_restore.setEnabled(has_bak)
        self.b_add.setEnabled(self._buf is not None)
        self._update_title()
        self.statusBar().showMessage(
            f"{path.name}  —  {len(elements)} elements, {total_frames} frames  |  "
            "click a bar to edit · delete/apply in the inspector"
        )

    def _update_title(self):
        name = self._current_file.name if self._current_file else "—"
        star = " *" if self._dirty else ""
        self.setWindowTitle(f"FF16 Timeline Viewer — {name}{star}")

    def _on_elem_selected(self, elem):
        if self._buf is None:
            self.inspector.show_element(elem)
            return
        try:
            sections = tlb_encode.build_field_map(self._buf, elem["index"])
        except Exception as e:
            self.statusBar().showMessage(f"Field map failed: {e}")
            sections = None
        self.inspector.show_element(elem, sections)

    # ── editing / delete / save / restore ──────────────────────────────────

    def _apply_patches(self, patches):
        """patches: list of (offset, fmt, value). Patch buffer in place."""
        if self._buf is None:
            return
        try:
            for offset, fmt, value in patches:
                tlb_encode.patch_scalar(self._buf, offset, fmt, value)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Patch failed", str(e))
            return
        self._dirty = True
        # remember which element was selected so we can reselect it
        sel = self.inspector._cur_elem
        sel_idx = sel["index"] if sel else None
        self._render_from_buf()
        self._reselect(sel_idx)
        self.statusBar().showMessage(
            f"Applied {len(patches)} change(s) — not yet saved (Ctrl+S)")

    def _delete_element(self, index):
        if self._buf is None:
            return
        try:
            self._buf = bytearray(tlb_encode.delete_element(self._buf, index))
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Delete failed", str(e))
            return
        self._dirty = True
        self._render_from_buf()
        self.statusBar().showMessage(
            f"Deleted element #{index} — not yet saved (Ctrl+S)")

    def _add_element_dialog(self):
        if self._buf is None:
            return
        labels = [tlb_encode.type_label(t) for t in tlb_encode.ADDABLE_TYPES]
        # default to BulletTimeRange (12) if present
        default_idx = (tlb_encode.ADDABLE_TYPES.index(12)
                       if 12 in tlb_encode.ADDABLE_TYPES else 0)
        label, ok = QtWidgets.QInputDialog.getItem(
            self, "Add timeline element",
            "Element type to add (scalar-only types):",
            labels, default_idx, editable=False)
        if not ok:
            return
        union_type = tlb_encode.ADDABLE_TYPES[labels.index(label)]
        try:
            new_buf, new_idx = tlb_encode.add_element(self._buf, union_type)
            self._buf = bytearray(new_buf)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Add failed", str(e))
            return
        self._dirty = True
        self._render_from_buf()
        self._reselect(new_idx)         # open the new element's editor
        self.statusBar().showMessage(
            f"Added {tlb_encode.type_label(union_type)} as element #{new_idx} "
            "— set its frame range/fields, then Save (Ctrl+S)")

    def _reselect(self, index):
        """Reselect element `index` in the timeline after a rebuild."""
        if index is None:
            return
        for bar in self.timeline._bars:
            if bar.elem["index"] == index:
                self.timeline._on_bar_click(bar)
                break

    def _save_current(self):
        if self._buf is None or self._current_file is None:
            return
        if not self._dirty:
            self.statusBar().showMessage("No changes to save.")
            return
        try:
            # back up the untouched original once, then overwrite in place
            bak = tlb_encode.ensure_backup(self._current_file)
            with open(self._current_file, "wb") as fh:
                fh.write(self._buf)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Save failed", str(e))
            return
        self._dirty = False
        self._render_from_buf()
        self.statusBar().showMessage(
            f"Saved {self._current_file.name}  (original backed up as "
            f"{Path(bak).name})")

        # mirror into PackBack and repack into the .diff.pac mod archive
        self._pack_current()

    def _pack_current(self):
        """Copy the saved .tlb into PackBack (relative to the raw-data root) and run
        FF16Tools.CLI to build the .diff.pac. Non-fatal on failure — the file
        is already saved in place."""
        src = self._current_file
        try:
            rel = src.resolve().relative_to(RAW_ROOT.resolve())
        except ValueError:
            self.statusBar().showMessage(
                f"Saved — skipped repack ({src.name} is outside {RAW_ROOT})")
            return

        dest = PACKBACK_DIR / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "PackBack copy failed", str(e))
            return

        if not FF16TOOLS_CLI.exists():
            QtWidgets.QMessageBox.warning(
                self, "Pack skipped",
                f"Copied to PackBack but CLI not found:\n{FF16TOOLS_CLI}")
            return

        # remove stale archive first so the pack always produces a fresh one
        try:
            if DIFF_PAC.exists():
                DIFF_PAC.unlink()
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Pack failed", str(e))
            return

        self.statusBar().showMessage(f"Packing {DIFF_PAC.name} …")
        QtWidgets.QApplication.processEvents()
        cmd = [str(FF16TOOLS_CLI), "pack",
               "-i", str(PACKBACK_DIR), "-o", str(DIFF_PAC)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  cwd=str(PACKBACK_DIR.parent))
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Pack failed",
                                          f"{e}\n\n{' '.join(cmd)}")
            return

        if proc.returncode != 0 or not DIFF_PAC.exists():
            tail = (proc.stderr or proc.stdout or "").strip()[-2000:]
            QtWidgets.QMessageBox.warning(
                self, "Pack failed",
                f"FF16Tools.CLI exited with code {proc.returncode}.\n\n{tail}")
            self.statusBar().showMessage("Pack failed — see dialog.")
            return

        self.statusBar().showMessage(
            f"Saved {src.name} → PackBack\\{rel}  ·  packed {DIFF_PAC.name}")

    def _restore_current(self):
        if self._current_file is None:
            return
        if not tlb_encode.has_backup(self._current_file):
            QtWidgets.QMessageBox.information(
                self, "Restore",
                "No backup (*_origin.tlb) exists for this file.")
            return
        resp = QtWidgets.QMessageBox.question(
            self, "Restore original",
            f"Restore {self._current_file.name} from its "
            f"{tlb_encode.origin_path(self._current_file).name} backup?\n\n"
            "This overwrites the current file with the original and "
            "discards all edits.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if resp != QtWidgets.QMessageBox.Yes:
            return
        try:
            tlb_encode.restore_backup(self._current_file)
            with open(self._current_file, "rb") as fh:
                self._buf = bytearray(fh.read())
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Restore failed", str(e))
            return
        self._dirty = False
        self._render_from_buf()
        self.statusBar().showMessage(
            f"Restored {self._current_file.name} from backup.")

    def closeEvent(self, event):
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()


# ─────────────────────────────────────────────── entry point

def main(argv):
    app = QtWidgets.QApplication(argv)
    app.setStyle("Fusion")

    # dark palette
    pal = QtGui.QPalette()
    dark = QtGui.QColor("#1e1e24")
    mid  = QtGui.QColor("#2a2a32")
    text = QtGui.QColor("#e2e2e8")
    pal.setColor(QtGui.QPalette.Window,          dark)
    pal.setColor(QtGui.QPalette.WindowText,      text)
    pal.setColor(QtGui.QPalette.Base,            mid)
    pal.setColor(QtGui.QPalette.AlternateBase,   QtGui.QColor("#242430"))
    pal.setColor(QtGui.QPalette.Text,            text)
    pal.setColor(QtGui.QPalette.Button,          mid)
    pal.setColor(QtGui.QPalette.ButtonText,      text)
    pal.setColor(QtGui.QPalette.Highlight,       QtGui.QColor("#3a6ea5"))
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor("#ffffff"))
    app.setPalette(pal)

    start = argv[1] if len(argv) > 1 else None
    win = MainWindow(start)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
