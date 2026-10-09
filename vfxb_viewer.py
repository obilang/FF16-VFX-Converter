"""
FF16 .vfxb graph visualizer (PySide6).

A GUI front-end for vfxb_decode.py. Loads a .vfxb (or a whole directory of
them) and draws the effect node-graph as an interactive tree:

  - Left  : file list (open a single file or a directory)
  - Center: node graph  - pan (drag), zoom (wheel), click a node to inspect
  - Right : node inspector - hash, role, raw properties, and inline plots of
            every animation curve (keylist) the node carries

Node colouring:
  - grey/blue  container node (has children, carries no curves)
  - green      leaf node with animation curves
  - amber      leaf node that references a texture group

Run:
    python vfxb_viewer.py [path.vfxb | directory]
"""

import math
import sys
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

import vfxb_decode
import vfxb_encode
from path_settings import PathSettingsDialog


# ----------------------------------------------------------------- layout data

NODE_W = 268           # card width
H_GAP = 70             # horizontal gap between depth levels
V_GAP = 18             # vertical gap between sibling subtrees

MAX_RECENT = 12        # recent files/folders remembered in the File menu

PAD = 9                # inner card padding
HEADER_H = 40          # hash + meta band
SECTION_H = 17         # a "Textures"/"Scalars"/... section header row
ROW_H = 14             # one text row (texture path, scalar, other prop)
CURVE_H = 34           # a mini sparkline block (incl. its label line)
FOOT_PAD = 8


def _finite(v):
    return isinstance(v, (int, float)) and math.isfinite(v)


def curve_points(keys):
    """Return plottable (time, value) points from a keylist.

    Only scalar-valued keys are plotted (vector keys are skipped). A trailing
    key with time == +inf is the "hold last value" end-sentinel: it is clamped
    to just past the last finite time so the hold segment is drawn instead of
    collapsing the whole curve onto the Y axis.
    """
    scalar = [(k.get("time"), k.get("value")) for k in keys
              if isinstance(k.get("value"), (int, float))]
    finite = [(t, v) for t, v in scalar if _finite(t) and _finite(v)]
    has_inf_tail = any(isinstance(t, float) and math.isinf(t)
                       for t, _ in scalar)
    if has_inf_tail and finite:
        tmax = max(t for t, _ in finite)
        span = tmax - min(t for t, _ in finite)
        hold_t = tmax + (span * 0.15 if span > 0 else 1.0)
        # value held past the end = value of the last finite key
        finite.append((hold_t, finite[-1][1]))
    return finite


# Colours used to draw the channels of a detected vector group.
CHANNEL_COLORS = ["#e05555", "#5fd08a", "#5b9bd0", "#d0b055"]  # R G B A / X Y Z W
VEC_LABELS = {2: ["X", "Y"], 3: ["R", "G", "B"], 4: ["R", "G", "B", "A"]}


def _time_sig(keylist_prop):
    """A signature of a curve's keyframe TIMES (ignores values)."""
    return tuple(
        round(k["time"], 3) if isinstance(k["time"], (int, float))
        and not (isinstance(k["time"], float) and math.isinf(k["time"]))
        else "inf"
        for k in keylist_prop["keys"]["keys"]
    )


def group_curves(curves):
    """Group curves that animate one vec2/vec3/vec4 parameter.

    Primary signal (reliable): the binding `target_offset` - the byte offset of
    the animated value in the item's parameter block. Consecutive curves whose
    offsets are 4 bytes apart (a float stride) and share the same binding
    `target_flags` are the per-channel tracks of one vector. This is what the
    file actually encodes, so it beats the old time-signature guess.

    Fallback (older data or missing offsets): group adjacent curves that share
    the same keyframe-time signature.

    Returns a list of groups; each group is a list of curve props (length 1 =
    scalar, 2-4 = vector).
    """
    groups = []
    i = 0
    n = len(curves)
    while i < n:
        off = curves[i].get("target_offset")
        flags = curves[i].get("target_flags")
        j = i + 1
        if off is not None:
            # extend while offsets step by +4 with matching flags (contiguous
            # float components of one vector), capped at 4 (vec4).
            while (j < n and j - i < 4
                   and curves[j].get("target_offset") == off + 4 * (j - i)
                   and curves[j].get("target_flags") == flags):
                j += 1
        else:
            sig = _time_sig(curves[i])
            while (j < n and j - i < 4
                   and curves[j].get("target_offset") is None
                   and _time_sig(curves[j]) == sig):
                j += 1
        groups.append(curves[i:j])
        i = j
    return groups


def build_rows(data):
    """Flatten a node's contents into an ordered list of drawable rows.

    Each row is a dict with a 'kind' the painter understands. This is the
    single source of truth for BOTH card-height computation and painting,
    so the two can never disagree.
    """
    props = data.get("properties", [])
    curves = [p for p in props if p.get("kind") == "keylist"]
    scalars = [p for p in props if p.get("kind") in ("scalar", "scalar2")]
    textures = [p for p in props if p.get("kind") == "texture_group"]
    # 0x31 props that carry NO texture group (pure distortion/framebuffer passes)
    tex_slots = [p for p in props if p.get("kind") == "texture_slots"]
    others = [p for p in props if p.get("kind") in (None, "generic",
                                                    "item_group")]
    rows = []

    shader = data.get("shader")
    if shader:
        programs = shader.get("program_indices", [])
        particle_order = shader.get("particle_order")
        group_order = shader.get("shader_group_order")
        rows.append({"kind": "section", "text": "Shader (order inferred)"})
        if group_order is None:
            text = f"particle #{particle_order + 1}: no matching shader group"
        else:
            plist = ", ".join(str(p) for p in programs) or "none"
            text = (f"particle #{particle_order + 1} → group "
                    f"#{group_order + 1} (programs {plist})")
        rows.append({"kind": "text", "text": text, "color": "#8fd0ff"})

    model = data.get("model")
    if model:
        rows.append({"kind": "section", "text": "Model (inferred)"})
        rows.append({"kind": "text", "text": "• " + model.split("/")[-1],
                     "color": "#d9a7ff", "full": model})

    if textures:
        rows.append({"kind": "section", "text": "Textures"})
        for p in textures:
            for tex in p["group"]["textures"]:
                short = tex.split("/")[-1]
                rows.append({"kind": "text", "text": "• " + short,
                             "color": "#ffcf70", "full": tex})

    if tex_slots:
        rows.append({"kind": "section", "text": "Textures"})
        for _p in tex_slots:
            rows.append({"kind": "text", "text": "(no texture group)",
                         "color": "#8a8a92"})

    if scalars:
        rows.append({"kind": "section", "text": "Scalars"})
        for p in scalars:
            val = p.get("value", p.get("values"))
            if isinstance(val, list):
                vs = ", ".join(f"{v:.4g}" if _finite(v) else str(v)
                               for v in val)
            elif _finite(val):
                vs = f"{val:.4g}"
            else:
                vs = str(val)
            off = p.get("target_offset")
            tag = f" @{off}" if off is not None else ""
            rows.append({"kind": "text", "text": f"{p['type']}{tag} = {vs}",
                         "color": "#cfe8ff"})

    if curves:
        groups = group_curves(curves)
        rows.append({"kind": "section", "text": f"Curves ({len(curves)})"})
        idx = 0
        for grp in groups:
            n = len(grp)
            bind = _binding_tag(grp)
            if n >= 2:
                # vector group -> one overlaid multi-channel plot
                labels = VEC_LABELS.get(n, [str(k) for k in range(n)])
                # a group of identical curves is a uniform vector (e.g. scale)
                uniform = _group_uniform(grp)
                kind_lbl = ("uniform vec%d" % n) if uniform else ("vec%d" % n)
                rows.append({
                    "kind": "vcurve",
                    "channels": [c["keys"]["keys"] for c in grp],
                    "labels": labels,
                    "label": f"{grp[0]['type']} {bind}  {kind_lbl}".strip(),
                })
            else:
                rows.append({"kind": "curve", "keys": grp[0]["keys"]["keys"],
                             "label": f"{grp[0]['type']} {bind} "
                                      f"({grp[0]['keys']['count']} keys)"
                                      .strip()})
            idx += n

    if others:
        rows.append({"kind": "section", "text": f"Other ({len(others)})"})
        for p in others:
            bits = [p["type"]]
            if p.get("kind") == "item_group":
                bits.append(f"spawn → {p.get('child_count', 0)} "
                            f"(n={p.get('spawn_count', '?')})")
            elif "as_floats" in p:
                fl = p["as_floats"]
                shown = ", ".join(f"{v:.4g}" for v in fl[:4])
                if len(fl) > 4:
                    shown += " …"
                bits.append(f"[{shown}]")
            elif "raw" in p:
                bits.append(p["raw"][:16])
            rows.append({"kind": "text", "text": "  ".join(bits),
                         "color": "#9a9aa2"})

    return rows


def _binding_tag(grp):
    """Short '@<offset>' binding tag for a curve group (empty if unknown).

    The offset is the byte position in the item's parameter block that the
    curve drives - the closest thing the file has to a module identity.
    """
    off = grp[0].get("target_offset")
    if off is None:
        return ""
    flags = grp[0].get("target_flags")
    if len(grp) >= 2:
        end = off + 4 * (len(grp) - 1)
        span = f"@{off}-{end}"
    else:
        span = f"@{off}"
    return f"{span} f{flags:#x}" if flags is not None else span


def _group_uniform(grp):
    """True if every channel in a group has identical values (uniform vector)."""
    def vals(c):
        return [round(k.get("value"), 5) for k in c["keys"]["keys"]
                if isinstance(k.get("value"), (int, float))]
    base = vals(grp[0])
    return all(vals(c) == base for c in grp[1:])


def row_height(row):
    if row["kind"] == "section":
        return SECTION_H
    if row["kind"] in ("curve", "vcurve"):
        return CURVE_H
    return ROW_H


def card_height(data):
    rows = build_rows(data)
    body = sum(row_height(r) for r in rows)
    return HEADER_H + body + FOOT_PAD


class GraphNode:
    """Lightweight layout wrapper around a decoded item dict."""

    __slots__ = ("data", "children", "x", "y", "h", "subtree_h", "depth")

    def __init__(self, data, depth):
        self.data = data
        self.depth = depth
        self.children = [GraphNode(c, depth + 1)
                         for c in data.get("children", [])
                         if "hash" in c]
        self.x = 0.0
        self.y = 0.0
        self.h = card_height(data)      # this node's own card height
        self.subtree_h = 0.0

    def layout(self, x, y_top):
        """Assign positions; left-to-right by depth, stacked vertically."""
        self.x = x
        if not self.children:
            self.subtree_h = self.h
            self.y = y_top
            return self.subtree_h
        cy = y_top
        for c in self.children:
            ch = c.layout(x + NODE_W + H_GAP, cy)
            cy += ch + V_GAP
        total = cy - V_GAP - y_top
        self.subtree_h = max(total, self.h)
        # center parent against its children block
        self.y = y_top + (self.subtree_h - self.h) / 2
        return self.subtree_h

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


# ----------------------------------------------------------------- node styling

def node_role(data):
    props = data.get("properties", [])
    has_curves = any(p.get("kind") == "keylist" for p in props)
    has_tex = any(p.get("kind") == "texture_group" for p in props)
    # a 0x31 with no group = a render pass that samples no texture (distortion)
    has_slots = any(p.get("kind") == "texture_slots" for p in props)
    has_children = bool(data.get("children"))
    if has_children and not has_curves:
        return "container"
    if has_tex:
        return "textured"
    if has_slots:
        return "distortion"
    if has_curves:
        return "curves"
    return "leaf"


ROLE_COLORS = {
    "container": QtGui.QColor("#3b5b78"),
    "curves": QtGui.QColor("#2e6b45"),
    "textured": QtGui.QColor("#8a6d1f"),
    # untextured render pass (framebuffer distortion) — violet, distinct from
    # the amber "textured" so a mis-assigned texture can't hide as one
    "distortion": QtGui.QColor("#6a4a8a"),
    "leaf": QtGui.QColor("#4a4a52"),
}


# ----------------------------------------------------------------- graph items

def _draw_sparkline(p, rect, keys):
    """Draw a compact curve sparkline inside rect (a QRectF)."""
    pts = curve_points(keys)
    p.fillRect(rect, QtGui.QColor("#20201f"))
    if not pts:
        p.setPen(QtGui.QColor("#777"))
        p.drawText(rect.adjusted(4, 0, 0, 0),
                   QtCore.Qt.AlignVCenter, "(vector / non-scalar keys)")
        return
    ts = [t for t, _ in pts]
    vs = [v for _, v in pts]
    tmin, tmax = min(ts), max(ts)
    vmin, vmax = min(vs), max(vs)
    if tmax - tmin < 1e-9:
        tmax = tmin + 1
    if vmax - vmin < 1e-9:
        vmin -= 0.5
        vmax += 0.5
    left = rect.left() + 4
    right = rect.right() - 34        # leave room for range label
    top = rect.top() + 3
    bot = rect.bottom() - 3

    def sx(t):
        return left + (t - tmin) / (tmax - tmin) * (right - left)

    def sy(v):
        return bot - (v - vmin) / (vmax - vmin) * (bot - top)

    path = QtGui.QPainterPath(QtCore.QPointF(sx(pts[0][0]), sy(pts[0][1])))
    for t, v in pts[1:]:
        path.lineTo(sx(t), sy(v))
    p.setPen(QtGui.QPen(QtGui.QColor("#5fd08a"), 1.5))
    p.drawPath(path)
    p.setBrush(QtGui.QColor("#ffd166"))
    p.setPen(QtCore.Qt.NoPen)
    for t, v in pts:
        p.drawEllipse(QtCore.QPointF(sx(t), sy(v)), 2.0, 2.0)
    # min/max labels on the right edge
    p.setPen(QtGui.QColor("#8a8a92"))
    f = p.font(); f.setPointSize(6); p.setFont(f)
    p.drawText(QtCore.QRectF(right + 2, top - 2, 34, 10),
               QtCore.Qt.AlignRight, f"{vmax:.3g}")
    p.drawText(QtCore.QRectF(right + 2, bot - 8, 34, 10),
               QtCore.Qt.AlignRight, f"{vmin:.3g}")


def _draw_vsparkline(p, rect, channels, labels):
    """Draw several channels overlaid in one sparkline (colour/vector group)."""
    p.fillRect(rect, QtGui.QColor("#20201f"))
    chan_pts = [curve_points(ch) for ch in channels]
    all_pts = [pt for pts in chan_pts for pt in pts]
    if not all_pts:
        p.setPen(QtGui.QColor("#777"))
        p.drawText(rect.adjusted(4, 0, 0, 0),
                   QtCore.Qt.AlignVCenter, "(no scalar keys)")
        return
    ts = [t for t, _ in all_pts]
    vs = [v for _, v in all_pts]
    tmin, tmax = min(ts), max(ts)
    vmin, vmax = min(vs), max(vs)
    if tmax - tmin < 1e-9:
        tmax = tmin + 1
    if vmax - vmin < 1e-9:
        vmin -= 0.5
        vmax += 0.5
    left = rect.left() + 4
    right = rect.right() - 34
    top = rect.top() + 3
    bot = rect.bottom() - 3

    def sx(t):
        return left + (t - tmin) / (tmax - tmin) * (right - left)

    def sy(v):
        return bot - (v - vmin) / (vmax - vmin) * (bot - top)

    for ci, pts in enumerate(chan_pts):
        if not pts:
            continue
        col = QtGui.QColor(CHANNEL_COLORS[ci % len(CHANNEL_COLORS)])
        path = QtGui.QPainterPath(QtCore.QPointF(sx(pts[0][0]), sy(pts[0][1])))
        for t, v in pts[1:]:
            path.lineTo(sx(t), sy(v))
        p.setPen(QtGui.QPen(col, 1.4))
        p.drawPath(path)
        p.setBrush(col)
        p.setPen(QtCore.Qt.NoPen)
        for t, v in pts:
            p.drawEllipse(QtCore.QPointF(sx(t), sy(v)), 1.6, 1.6)

    # range labels + channel legend on the right edge
    p.setPen(QtGui.QColor("#8a8a92"))
    f = p.font(); f.setPointSize(6); p.setFont(f)
    p.drawText(QtCore.QRectF(right + 2, top - 2, 34, 10),
               QtCore.Qt.AlignRight, f"{vmax:.3g}")
    p.drawText(QtCore.QRectF(right + 2, bot - 8, 34, 10),
               QtCore.Qt.AlignRight, f"{vmin:.3g}")


class NodeItem(QtWidgets.QGraphicsItem):
    """Custom-painted rich card showing all of a node's data inline."""

    def __init__(self, gnode, on_click):
        super().__init__()
        self.gnode = gnode
        self.on_click = on_click
        self.rows = build_rows(gnode.data)
        self.w = NODE_W
        self.h = gnode.h
        self.role = node_role(gnode.data)
        self._hover = False
        self.setPos(gnode.x, gnode.y)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.setZValue(1)
        self.setToolTip(self._tooltip())

    def _tooltip(self):
        d = self.gnode.data
        tip = (f"{d.get('hash','?')}  ({self.role})\n"
               f"children: {len(d.get('children', []))}   "
               f"properties: {len(d.get('properties', []))}")
        dur = d.get("duration")
        if isinstance(dur, (int, float)):
            rng = d.get("lifetime_range")
            tip += f"\nduration: {dur:g}"
            if isinstance(rng, (int, float)) and rng:
                tip += f"  (range +{rng:g})"
        return tip

    def boundingRect(self):
        return QtCore.QRectF(0, 0, self.w, self.h)

    def paint(self, p, option, widget=None):
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        base = ROLE_COLORS[self.role]
        rect = QtCore.QRectF(0, 0, self.w, self.h)

        # card body
        p.setBrush(QtGui.QBrush(QtGui.QColor("#2a2a30")))
        border = QtGui.QColor("#ffffff") if (self._hover or self.isSelected()) \
            else base.lighter(140)
        p.setPen(QtGui.QPen(border, 2 if self._hover else 1.4))
        p.drawRoundedRect(rect, 6, 6)

        # header band (role-coloured)
        header = QtCore.QRectF(0, 0, self.w, HEADER_H)
        path = QtGui.QPainterPath()
        path.addRoundedRect(header, 6, 6)
        p.fillPath(path, base)
        p.fillRect(QtCore.QRectF(0, HEADER_H - 8, self.w, 8), base)

        d = self.gnode.data
        p.setPen(QtGui.QColor("#ffffff"))
        f = p.font(); f.setBold(True); f.setPointSize(9); p.setFont(f)
        p.drawText(QtCore.QRectF(PAD, 4, self.w - 2 * PAD, 16),
                   QtCore.Qt.AlignVCenter, d.get("hash", "?"))

        f.setBold(False); f.setPointSize(7); p.setFont(f)
        p.setPen(QtGui.QColor("#e2e2e8"))
        nprops = len(d.get("properties", []))
        nchild = len(d.get("children", []))
        meta = f"{self.role}  ·  {nprops} props  ·  {nchild} children"
        dur = d.get("duration")
        if isinstance(dur, (int, float)):
            meta += f"  ·  {dur:g}f"
        p.drawText(QtCore.QRectF(PAD, 20, self.w - 2 * PAD, 14),
                   QtCore.Qt.AlignVCenter, meta)

        # body rows
        y = HEADER_H + 2
        for row in self.rows:
            rh = row_height(row)
            if row["kind"] == "section":
                p.setPen(QtGui.QColor("#8f8f98"))
                fs = p.font(); fs.setBold(True); fs.setPointSize(7)
                p.setFont(fs)
                p.drawText(QtCore.QRectF(PAD, y, self.w - 2 * PAD, rh),
                           QtCore.Qt.AlignVCenter, row["text"].upper())
                # divider line
                p.setPen(QtGui.QPen(QtGui.QColor("#3a3a42"), 1))
                p.drawLine(QtCore.QPointF(PAD, y + rh - 2),
                           QtCore.QPointF(self.w - PAD, y + rh - 2))
            elif row["kind"] == "curve":
                fs = p.font(); fs.setBold(False); fs.setPointSize(7)
                p.setFont(fs)
                p.setPen(QtGui.QColor("#c8c8c8"))
                p.drawText(QtCore.QRectF(PAD, y, self.w - 2 * PAD, 11),
                           QtCore.Qt.AlignVCenter, row["label"])
                spark = QtCore.QRectF(PAD, y + 11, self.w - 2 * PAD, rh - 12)
                _draw_sparkline(p, spark, row["keys"])
            elif row["kind"] == "vcurve":
                fs = p.font(); fs.setBold(False); fs.setPointSize(7)
                p.setFont(fs)
                # label + coloured channel legend
                p.setPen(QtGui.QColor("#c8c8c8"))
                p.drawText(QtCore.QRectF(PAD, y, self.w - 2 * PAD, 11),
                           QtCore.Qt.AlignVCenter, row["label"])
                lx = self.w - PAD
                for ci in reversed(range(len(row["labels"]))):
                    lab = row["labels"][ci]
                    lx -= 16
                    p.setPen(QtGui.QColor(
                        CHANNEL_COLORS[ci % len(CHANNEL_COLORS)]))
                    p.drawText(QtCore.QRectF(lx, y, 16, 11),
                               QtCore.Qt.AlignVCenter, lab)
                spark = QtCore.QRectF(PAD, y + 11, self.w - 2 * PAD, rh - 12)
                _draw_vsparkline(p, spark, row["channels"], row["labels"])
            else:  # text
                fs = p.font(); fs.setBold(False); fs.setPointSize(7)
                p.setFont(fs)
                p.setPen(QtGui.QColor(row.get("color", "#c0c0c8")))
                txt = row["text"]
                metrics = p.fontMetrics()
                txt = metrics.elidedText(txt, QtCore.Qt.ElideRight,
                                         int(self.w - 2 * PAD))
                p.drawText(QtCore.QRectF(PAD, y, self.w - 2 * PAD, rh),
                           QtCore.Qt.AlignVCenter, txt)
            y += rh

    def mousePressEvent(self, event):
        self.on_click(self.gnode.data)
        self.update()
        super().mousePressEvent(event)

    def hoverEnterEvent(self, event):
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)


class GraphView(QtWidgets.QGraphicsView):
    """Pan-with-drag, zoom-with-wheel canvas."""

    def __init__(self):
        super().__init__()
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setRenderHint(QtGui.QPainter.Antialiasing)
        self.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(QtGui.QColor("#1e1e22"))
        self._zoom = 1.0

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        new_zoom = self._zoom * factor
        if 0.05 < new_zoom < 8:
            self._zoom = new_zoom
            self.scale(factor, factor)

    def build(self, roots, on_click):
        scene = self.scene()
        scene.clear()

        edge_pen = QtGui.QPen(QtGui.QColor("#6a6a72"), 1.4)
        edge_pen.setCosmetic(True)

        y_cursor = 0.0
        for root in roots:
            root.layout(0, y_cursor)
            y_cursor += root.subtree_h + V_GAP * 3

        for root in roots:
            for gn in root.walk():
                for c in gn.children:
                    x1 = gn.x + NODE_W
                    y1 = gn.y + gn.h / 2
                    x2 = c.x
                    y2 = c.y + c.h / 2
                    mid = (x1 + x2) / 2
                    path = QtGui.QPainterPath(QtCore.QPointF(x1, y1))
                    path.cubicTo(mid, y1, mid, y2, x2, y2)
                    edge = scene.addPath(path, edge_pen)
                    edge.setZValue(0)
                scene.addItem(NodeItem(gn, on_click))

        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-60, -60, 60, 60))
        self.resetTransform()
        self._zoom = 1.0
        self.centerOn(0, 0)


# ----------------------------------------------------------------- curve plot

class CurvePlot(QtWidgets.QWidget):
    """Plot of one or more channels (time, value) with labelled axes.

    `channels` is a list of key-lists; a single-element list draws a scalar
    curve, 2-4 channels draw an overlaid vector/colour curve.
    """

    def __init__(self, channels, label, labels=None):
        super().__init__()
        self.channels = channels
        self.label = label
        self.labels = labels or (["" ] if len(channels) == 1 else
                                 [str(i) for i in range(len(channels))])
        self.setFixedHeight(140)
        self.setMinimumWidth(240)

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor("#26262b"))

        chan_pts = [curve_points(ch) for ch in self.channels]
        all_pts = [pt for pts in chan_pts for pt in pts]
        p.setPen(QtGui.QColor("#c8c8c8"))
        p.drawText(6, 14, self.label)

        if len(all_pts) < 1:
            p.setPen(QtGui.QColor("#888"))
            p.drawText(6, 40, "(non-scalar keys — not plottable)")
            return

        ts = [t for t, _ in all_pts]
        vs = [v for _, v in all_pts]
        tmin, tmax = min(ts), max(ts)
        vmin, vmax = min(vs), max(vs)
        if tmax - tmin < 1e-9:
            tmax = tmin + 1
        if vmax - vmin < 1e-9:
            vmin -= 0.5
            vmax += 0.5

        # plot area: leave a left gutter for value labels, bottom for time
        top = 22
        bot = self.height() - 20
        left = 44
        right = self.width() - 12

        def sx(t):
            return left + (t - tmin) / (tmax - tmin) * (right - left)

        def sy(v):
            return bot - (v - vmin) / (vmax - vmin) * (bot - top)

        # axes
        p.setPen(QtGui.QPen(QtGui.QColor("#4a4a52"), 1))
        p.drawLine(int(left), int(top), int(left), int(bot))   # Y axis
        p.drawLine(int(left), int(bot), int(right), int(bot))  # X axis

        # Y (value) gridlines + labels: min, mid, max
        for frac in (0.0, 0.5, 1.0):
            val = vmin + (vmax - vmin) * frac
            yy = sy(val)
            p.setPen(QtGui.QPen(QtGui.QColor("#333339"), 1, QtCore.Qt.DotLine))
            p.drawLine(int(left), int(yy), int(right), int(yy))
            p.setPen(QtGui.QColor("#8a8a92"))
            p.drawText(QtCore.QRectF(0, yy - 7, left - 4, 14),
                       QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
                       f"{val:.3g}")

        # X (time) tick labels at each real keyframe time (across all channels)
        p.setPen(QtGui.QColor("#8a8a92"))
        fx = p.font(); fx.setPointSize(7); p.setFont(fx)
        seen_x = []
        for t in sorted(set(ts)):
            x = sx(t)
            if any(abs(x - sx0) < 26 for sx0 in seen_x):
                continue          # avoid overlapping labels
            seen_x.append(x)
            p.setPen(QtGui.QPen(QtGui.QColor("#333339"), 1, QtCore.Qt.DotLine))
            p.drawLine(int(x), int(top), int(x), int(bot))
            p.setPen(QtGui.QColor("#8a8a92"))
            p.drawText(QtCore.QRectF(x - 20, bot + 2, 40, 14),
                       QtCore.Qt.AlignHCenter, f"{t:g}")

        # each channel
        legend_x = right
        for ci, pts in enumerate(chan_pts):
            if not pts:
                continue
            col = QtGui.QColor(CHANNEL_COLORS[ci % len(CHANNEL_COLORS)]) \
                if len(chan_pts) > 1 else QtGui.QColor("#5fd08a")
            path = QtGui.QPainterPath(QtCore.QPointF(sx(pts[0][0]),
                                                     sy(pts[0][1])))
            for t, v in pts[1:]:
                path.lineTo(sx(t), sy(v))
            p.setPen(QtGui.QPen(col, 1.8))
            p.drawPath(path)
            dot = QtGui.QColor("#ffd166") if len(chan_pts) == 1 else col
            p.setBrush(dot)
            p.setPen(QtCore.Qt.NoPen)
            for t, v in pts:
                p.drawEllipse(QtCore.QPointF(sx(t), sy(v)), 2.6, 2.6)
            # legend swatch for multi-channel
            if len(chan_pts) > 1 and self.labels[ci]:
                legend_x -= 18
                p.setPen(col)
                fl = p.font(); fl.setPointSize(8); fl.setBold(True); p.setFont(fl)
                p.drawText(QtCore.QRectF(legend_x, 4, 18, 14),
                           QtCore.Qt.AlignCenter, self.labels[ci])


# -------------------------------------------------------------- shader viewer

def _shader_sources(shader):
    """Return unique converted source entries from a node shader annotation."""
    sources = []
    seen = set()
    for program in shader.get("programs", []):
        program_index = program.get("program")
        for stage in ("vertex", "pixel", "unknown"):
            for entry in program.get(stage, []):
                path = entry.get("glsl")
                if not path or path in seen:
                    continue
                seen.add(path)
                sources.append({
                    "path": path,
                    "stage": stage,
                    "shader_data_index": entry.get("shader_data_index"),
                    "program": program_index,
                })
    return sources


class ShaderViewerDialog(QtWidgets.QDialog):
    """Large, read-only GLSL source viewer with one tab per shader stage."""

    def __init__(self, shader, parent=None):
        super().__init__(parent)
        particle = shader.get("particle_order", 0) + 1
        self.setWindowTitle(f"Particle {particle} — converted GLSL")
        self.resize(1100, 780)
        lay = QtWidgets.QVBoxLayout(self)

        note = QtWidgets.QLabel(shader.get("note", ""))
        note.setWordWrap(True)
        note.setStyleSheet("color:#b9b9c0; padding:2px 4px;")
        lay.addWidget(note)

        tabs = QtWidgets.QTabWidget()
        for source in _shader_sources(shader):
            path = Path(source["path"])
            try:
                code = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                code = f"Could not read {path}\n\n{exc}"
            edit = QtWidgets.QPlainTextEdit(code)
            edit.setReadOnly(True)
            edit.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
            edit.setFont(QtGui.QFont("Consolas", 9))
            edit.setStyleSheet(
                "QPlainTextEdit{background:#16161a;color:#d8d8de;"
                "selection-background-color:#3b5b78;}"
            )
            stage = source["stage"].upper()
            data_index = source["shader_data_index"]
            tabs.addTab(edit, f"{stage} data #{data_index}")
            tabs.setTabToolTip(tabs.count() - 1, str(path))
        lay.addWidget(tabs)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)


# ----------------------------------------------------------------- inspector

class Inspector(QtWidgets.QScrollArea):
    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self._host = QtWidgets.QWidget()
        self._lay = QtWidgets.QVBoxLayout(self._host)
        self._lay.setAlignment(QtCore.Qt.AlignTop)
        self.setWidget(self._host)

        # editing state / callbacks (wired by MainWindow)
        self._apply_cb = None    # (list[(offset, fmt, value)]) -> None
        self._asset_apply_cb = None  # ({string_index: new_path}) -> None
        self._nav_cb = None      # (item_offset:int) -> None  jump to a node
        self._editors = []       # list of (offset, fmt, QLineEdit)

        self.show_placeholder()

    def set_callbacks(self, apply_cb, nav_cb=None, asset_apply_cb=None):
        self._apply_cb = apply_cb
        self._nav_cb = nav_cb
        self._asset_apply_cb = asset_apply_cb

    def _clear(self):
        # recursively remove widgets AND nested sub-layouts (the editor adds
        # QFormLayout/QHBoxLayout via addLayout(); a top-level-only sweep would
        # orphan their children so they keep painting over new content).
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

    def show_placeholder(self):
        self._clear()
        lbl = QtWidgets.QLabel("Select a node to inspect its properties.")
        lbl.setStyleSheet("color:#888; padding:12px;")
        self._lay.addWidget(lbl)

    def _heading(self, text, size=11, bold=True, color="#ffffff"):
        lbl = QtWidgets.QLabel(text)
        f = lbl.font(); f.setPointSize(size); f.setBold(bold); lbl.setFont(f)
        lbl.setStyleSheet(f"color:{color};")
        lbl.setWordWrap(True)
        return lbl

    @staticmethod
    def _fmt(v):
        if isinstance(v, float) and math.isinf(v):
            return "∞" if v > 0 else "-∞"
        if isinstance(v, (int, float)):
            return f"{v:.5g}"
        return str(v)

    def _divider(self):
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setStyleSheet("color:#3a3a44;")
        return line

    # ── editable-field UI (built from vfxb_encode field sections) ───────────

    def _make_editor(self, fld):
        """Build a QLineEdit for one scalar field and remember it for Apply."""
        edit = QtWidgets.QLineEdit()
        edit.setStyleSheet("font-family:Consolas; font-size:8pt;")
        val = fld["value"]
        edit.setText(f"{val:.6g}" if isinstance(val, float) else str(val))
        edit.setProperty("orig_text", edit.text())
        self._editors.append((fld["offset"], fld["fmt"], edit))
        return edit

    def _add_edit_sections(self, sections):
        """Render editable `sections` (from vfxb_encode) with an Apply button.

        Returns True if any editable field was shown.
        """
        if not sections:
            return False
        self._lay.addWidget(self._heading("Editable values", size=10))
        for sec in sections:
            self._lay.addWidget(self._heading(sec["title"], size=9,
                                              color="#b9b9c0"))
            # wrap each form in its own widget so the vertical layout reserves
            # the correct height (a bare nested QFormLayout under an AlignTop
            # QVBoxLayout collapses and rows overlap)
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
        return True

    def _on_apply_clicked(self):
        if not self._apply_cb:
            return
        patches = []
        for offset, fmt, edit in self._editors:
            text = edit.text()
            if text == edit.property("orig_text"):
                continue                      # unchanged — skip
            try:
                value = vfxb_encode.parse_value(fmt, text)
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

    def _key_table(self, channels, labels):
        """A small grid listing each keyframe's time and per-channel value(s).

        The sparkline shows the shape; this makes the exact value at every key
        readable, which the curve alone can't. One row per keyframe time, one
        value column per channel, plus a tangent column when present.
        """
        # union of all keyframe times across the channels, in order
        times = []
        for ch in channels:
            for k in ch:
                t = k.get("time")
                if t not in times:
                    times.append(t)
        # times may include the JSON sentinel strings "NaN"/"Infinity" (non-finite
        # keyframe markers); sort those last and keep numeric order otherwise.
        def _tkey(t):
            if isinstance(t, (int, float)):
                return (1 if math.isinf(t) else 0, float(t))
            return (2, 0.0)  # string sentinels (NaN/Infinity) go to the end
        times.sort(key=_tkey)

        # per-channel lookup: time -> key dict
        lut = [{k.get("time"): k for k in ch} for ch in channels]
        has_tan = any("tangents" in k for ch in channels for k in ch)

        cols = ["t"] + [f"{lab} value" if lab else "value" for lab in labels]
        if has_tan:
            cols += ["tan in", "tan out"]

        tbl = QtWidgets.QTableWidget(len(times), len(cols))
        tbl.setHorizontalHeaderLabels(cols)
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        tbl.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        tbl.setFocusPolicy(QtCore.Qt.NoFocus)
        tbl.setAlternatingRowColors(True)
        tbl.setStyleSheet(
            "QTableWidget { background:#20201f; alternate-background-color:#26262b;"
            " color:#d8d8de; gridline-color:#33333a; font-family:Consolas;"
            " font-size:8pt; border:1px solid #33333a; }"
            "QHeaderView::section { background:#2a2a30; color:#b9b9c0;"
            " border:0px; border-right:1px solid #33333a; padding:2px 4px; }"
        )
        tbl.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.Stretch)

        def cell(text, color):
            it = QtWidgets.QTableWidgetItem(text)
            it.setForeground(QtGui.QColor(color))
            it.setTextAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignRight)
            return it

        for r, t in enumerate(times):
            tbl.setItem(r, 0, cell(self._fmt(t), "#cfe8ff"))
            for ci in range(len(channels)):
                k = lut[ci].get(t)
                col = (CHANNEL_COLORS[ci % len(CHANNEL_COLORS)]
                       if len(channels) > 1 else "#5fd08a")
                v = k.get("value") if k else None
                tbl.setItem(r, 1 + ci, cell(self._fmt(v), col))
            if has_tan:
                # tangents come from the first channel that has them at this t
                tan = None
                for ci in range(len(channels)):
                    k = lut[ci].get(t)
                    if k and "tangents" in k:
                        tan = k["tangents"]
                        break
                tin = self._fmt(tan[0]) if tan else "—"
                tout = self._fmt(tan[1]) if tan else "—"
                tbl.setItem(r, 1 + len(channels), cell(tin, "#9a9aa2"))
                tbl.setItem(r, 2 + len(channels), cell(tout, "#9a9aa2"))

        # size the table to its content so it sits inline in the scroll column
        tbl.resizeRowsToContents()
        row_h = tbl.rowHeight(0) if times else 0
        header_h = tbl.horizontalHeader().height()
        tbl.setFixedHeight(header_h + row_h * len(times) + 2)
        tbl.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        tbl.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        return tbl

    def show_node(self, data):
        self._clear()
        role = node_role(data)
        self._lay.addWidget(self._heading(f"Node {data.get('hash','?')}"))
        meta = (f"role: {role}    children: {len(data.get('children', []))}    "
                f"props: {len(data.get('properties', []))}")
        self._lay.addWidget(self._heading(meta, size=8, bold=False,
                                          color="#b9b9c0"))

        shader = data.get("shader")
        if shader:
            self._show_shader(shader)

        # ── editable values (scalars + keyframes) ────────────────────────────
        if self._apply_cb is not None:
            try:
                sections = vfxb_encode.build_node_fields(data)
            except Exception:  # noqa: BLE001 - never let editor build break view
                sections = []
            if self._add_edit_sections(sections):
                self._lay.addWidget(self._divider())
        dur = data.get("duration")
        if isinstance(dur, (int, float)):
            base = data.get("lifetime_base")
            rng = data.get("lifetime_range")
            parts = [f"duration: {dur:g}"]
            if isinstance(base, (int, float)) and base != dur:
                parts.append(f"base: {base:g}")
            if isinstance(rng, (int, float)) and rng:
                parts.append(f"range: +{rng:g}")
            self._lay.addWidget(self._heading(
                "lifetime — " + "   ".join(parts),
                size=8, bold=False, color="#b9b9c0"))

        # curves first (the interesting data), then scalars, then the rest
        props = data.get("properties", [])
        curves = [p for p in props if p.get("kind") == "keylist"]
        scalars = [p for p in props if p.get("kind") in ("scalar", "scalar2")]
        textures = [p for p in props if p.get("kind") == "texture_group"]
        tex_slots = [p for p in props if p.get("kind") == "texture_slots"]
        spawn_edges = [p for p in props if p.get("kind") == "item_group"]
        others = [p for p in props if p.get("kind") in (None, "generic")]

        if spawn_edges:
            self._show_spawn_edges(spawn_edges, data)

        model = data.get("model")
        if model:
            binding = data.get("model_binding", {})
            index = data.get("model_string_index")
            primary = binding.get("primary_texture_string_index")
            self._lay.addWidget(self._heading("Model (inferred)", size=10))
            lab = QtWidgets.QLabel("• " + model)
            lab.setStyleSheet("color:#d9a7ff;")
            lab.setWordWrap(True)
            self._lay.addWidget(lab)
            meta = QtWidgets.QLabel(
                f"string[{index}] → primary texture string[{primary}]")
            meta.setStyleSheet("color:#8f8299; font-family:Consolas;"
                               " font-size:8pt;")
            self._lay.addWidget(meta)

        if textures:
            self._lay.addWidget(self._heading("Textures", size=10))
            for p in textures:
                for tex in p["group"]["textures"]:
                    lab = QtWidgets.QLabel("• " + tex)
                    lab.setStyleSheet("color:#ffcf70;")
                    lab.setWordWrap(True)
                    self._lay.addWidget(lab)

        if tex_slots:
            self._lay.addWidget(self._heading("Textures", size=10))
            for _p in tex_slots:
                lab = QtWidgets.QLabel(
                    "0x31 render pass with no texture group — samples the "
                    "framebuffer (distortion), not a texture.")
                lab.setStyleSheet("color:#b9a0d0; font-size:8pt;")
                lab.setWordWrap(True)
                self._lay.addWidget(lab)

        if scalars:
            self._lay.addWidget(self._heading("Scalars", size=10))
            for p in scalars:
                val = p.get("value", p.get("values"))
                off = p.get("target_offset")
                tag = f"  @{off}" if off is not None else ""
                lab = QtWidgets.QLabel(f"{p['type']}{tag}  =  {val}")
                lab.setStyleSheet("color:#cfe8ff; font-family:Consolas;")
                lab.setWordWrap(True)
                self._lay.addWidget(lab)

        if curves:
            self._lay.addWidget(self._heading(
                f"Animation curves ({len(curves)})", size=10))
            groups = group_curves(curves)
            idx = 0
            for grp in groups:
                n = len(grp)
                bind = _binding_tag(grp)
                if n >= 2:
                    labels = VEC_LABELS.get(n, [str(k) for k in range(n)])
                    uniform = _group_uniform(grp)
                    tag = ("uniform vec%d" % n) if uniform else ("vec%d" % n)
                    channels = [c["keys"]["keys"] for c in grp]
                    self._lay.addWidget(CurvePlot(
                        channels, f"{grp[0]['type']}  {bind}  {tag}".strip(),
                        labels=labels))
                    self._lay.addWidget(self._key_table(channels, labels))
                else:
                    keys = grp[0]["keys"]["keys"]
                    self._lay.addWidget(CurvePlot(
                        [keys],
                        f"{grp[0]['type']}  {bind}  "
                        f"({grp[0]['keys']['count']} keys)".strip()))
                    self._lay.addWidget(self._key_table([keys], ["value"]))
                idx += n

        if others:
            self._lay.addWidget(self._heading(
                f"Other properties ({len(others)})", size=10))
            for p in others:
                bits = [p["type"]]
                if p.get("kind"):
                    bits.append(p["kind"])
                if "child_count" in p:
                    bits.append(f"children={p['child_count']}")
                if "as_floats" in p:
                    bits.append("floats=" + str(p["as_floats"]))
                elif "raw" in p:
                    bits.append("raw=" + p["raw"])
                lab = QtWidgets.QLabel("  ".join(bits))
                lab.setStyleSheet("color:#9a9aa2; font-family:Consolas; "
                                  "font-size:8pt;")
                lab.setWordWrap(True)
                self._lay.addWidget(lab)

    def _show_shader(self, shader):
        """Show the inferred ordered mapping and open converted GLSL in-app."""
        particle = shader.get("particle_order")
        group = shader.get("shader_group_order")
        programs = shader.get("program_indices", [])
        self._lay.addWidget(self._heading("Shader (order inferred)", size=10))

        if group is None:
            mapping = f"particle #{particle + 1} → no matching shader group"
        else:
            plist = ", ".join(str(p) for p in programs) or "none"
            mapping = (f"particle #{particle + 1} → shader group #{group + 1}"
                       f"  ·  programs {plist}")
        lab = QtWidgets.QLabel(mapping)
        lab.setStyleSheet("color:#8fd0ff; font-family:Consolas;")
        lab.setWordWrap(True)
        self._lay.addWidget(lab)

        note = QtWidgets.QLabel(shader.get("note", ""))
        note.setStyleSheet("color:#8a8a92; font-size:8pt;")
        note.setWordWrap(True)
        self._lay.addWidget(note)

        sources = _shader_sources(shader)
        if sources:
            btn = QtWidgets.QPushButton(
                f"View GLSL source…  ({len(sources)} shader(s))")
            btn.setStyleSheet(
                "QPushButton{background:#243f55;color:#cfe8ff;"
                "font-weight:bold;}QPushButton:hover{background:#30536f;}"
            )
            btn.clicked.connect(
                lambda _checked=False, s=shader:
                ShaderViewerDialog(s, self).exec())
            self._lay.addWidget(btn)
        else:
            missing = QtWidgets.QLabel("No converted GLSL source is available.")
            missing.setStyleSheet("color:#c08080; font-size:8pt;")
            self._lay.addWidget(missing)

        self._lay.addWidget(self._divider())

    def _show_spawn_edges(self, edges, node):
        """Show each 0x2B spawn edge: what it spawns + its interval/count curves.

        A spawn edge connects this container to a spawned particle/emitter. Each
        edge's item_info `child_offset` resolves to one of this node's child
        nodes (looked up below), so we can name the spawned node's hash — and two
        edges pointing at the same offset means the same particle is spawned
        twice (confirmed in a00s_fire_buil_scaffold04_y: edge 0 and edge 4 both
        spawn 0x0AC6BF6E). The f6/f7 "use curve" spawn-interval / spawn-count
        curves are plotted like any other animation curve.
        """
        # map child item offset -> child node dict (for naming spawn targets)
        off_to_child = {}
        for c in node.get("children", []):
            if isinstance(c, dict) and "offset" in c:
                off_to_child.setdefault(c["offset"], c)

        self._lay.addWidget(self._heading(
            f"Spawn edges ({len(edges)})", size=10))
        mono = ("color:#c9d6a3; font-family:Consolas; font-size:8pt;")
        dim = ("color:#8a8a92; font-family:Consolas; font-size:8pt;")
        for i, p in enumerate(edges):
            # "edge i →  [target]" row, with each target a button that jumps to
            # the spawned node in the graph (child_offset resolves to a child).
            row = QtWidgets.QWidget()
            hl = QtWidgets.QHBoxLayout(row)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(4)
            arrow = QtWidgets.QLabel(f"edge {i}  →")
            arrow.setStyleSheet(mono)
            hl.addWidget(arrow)
            info = p.get("item_info") or []
            if not info:
                none_lab = QtWidgets.QLabel("(none)")
                none_lab.setStyleSheet(dim)
                hl.addWidget(none_lab)
            for e in info:
                off = e.get("child_offset")
                child = off_to_child.get(off)
                w = e.get("weight")
                wtag = (f" w={w:g}" if isinstance(w, (int, float)) and w != 1
                        else "")
                if child is not None and child.get("hash") \
                        and self._nav_cb is not None:
                    btn = QtWidgets.QPushButton(child.get("hash") + wtag)
                    btn.setCursor(QtCore.Qt.PointingHandCursor)
                    btn.setStyleSheet(
                        "QPushButton{color:#8fd0ff; background:transparent; "
                        "border:none; font-family:Consolas; font-size:8pt; "
                        "text-decoration:underline; padding:0;}"
                        "QPushButton:hover{color:#c0e8ff;}")
                    btn.clicked.connect(
                        lambda _=False, o=off: self._nav_cb(o))
                    hl.addWidget(btn)
                else:
                    name = (child.get("hash") if child and child.get("hash")
                            else f"off={off}")
                    t = QtWidgets.QLabel(name + wtag)
                    t.setStyleSheet(mono)
                    hl.addWidget(t)
            hl.addStretch(1)
            self._lay.addWidget(row)

            meta = QtWidgets.QLabel(
                f"    spawn_count={p.get('spawn_count')}  "
                f"slot={p.get('slot')}  f4a={p.get('field_0x4a')}  "
                f"f5={p.get('field_0x5')}")
            meta.setStyleSheet(dim)
            meta.setWordWrap(True)
            self._lay.addWidget(meta)

            raw = (f"    offsets: f6(interval)={p.get('f6_offset')}  "
                   f"f7(count)={p.get('f7_offset')}  "
                   f"f8={p.get('f8')}  f9={p.get('f9')}")
            l3 = QtWidgets.QLabel(raw)
            l3.setStyleSheet(dim)
            l3.setWordWrap(True)
            self._lay.addWidget(l3)

            # Four param-curve slots. Each run is bounded by the next-higher of
            # the four offsets, so a value is a clean single number unless the
            # param genuinely animates. "not set" (-1) means the editor left it at
            # its default (defaults aren't stored). f8/f9 meaning still unknown.
            for label, key in (("spawn interval", "interval_curve"),
                               ("spawn count", "count_curve"),
                               ("f8", "f8_curve"),
                               ("f9", "f9_curve")):
                curve = p.get(key)
                if not (curve and curve.get("keys")):
                    continue
                primary = curve.get("primary")
                extra = curve.get("extra") or []
                head = (f"    {label} = {primary:g}"
                        if isinstance(primary, (int, float))
                        else f"    {label} = {primary}")
                if extra:
                    etxt = ", ".join(f"{v:g}" if isinstance(v, (int, float))
                                     else str(v) for v in extra)
                    head += f"   (+{len(extra)} more: {etxt})"
                cl = QtWidgets.QLabel(head)
                cl.setStyleSheet(mono)
                cl.setWordWrap(True)
                self._lay.addWidget(cl)
                # plot only when the param genuinely has multiple keyframes
                if len(curve["keys"]) >= 2:
                    self._lay.addWidget(CurvePlot(
                        [curve["keys"]], f"edge {i}  {label}"))

    def show_constants(self, sections):
        """Show the file-global shader-constant editor (not tied to a node)."""
        self._clear()
        self._lay.addWidget(self._heading("Shader constants"))
        if not sections:
            self._lay.addWidget(self._heading(
                "No editable (float) constants in this file.",
                size=8, bold=False, color="#b9b9c0"))
            return
        self._add_edit_sections(sections)

    def show_assets(self, entries):
        """Show the file-global texture/model string-table editor."""
        self._clear()
        self._lay.addWidget(self._heading("Asset paths"))
        note = QtWidgets.QLabel(
            "Edits rebuild the VFXB string blob. Texture entries keep their "
            "string-table indices; model entries remain in the load manifest.")
        note.setStyleSheet("color:#b9b9c0; font-size:8pt;")
        note.setWordWrap(True)
        self._lay.addWidget(note)

        editors = []
        form_host = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(form_host)
        form.setContentsMargins(6, 6, 6, 6)
        form.setSpacing(5)
        form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        for entry in entries:
            edit = QtWidgets.QLineEdit(entry["value"])
            edit.setStyleSheet("font-family:Consolas; font-size:8pt;")
            edit.setProperty("orig_text", entry["value"])
            kind = entry.get("kind") or "asset"
            label = QtWidgets.QLabel(f'{kind} [{entry["index"]}]')
            label.setStyleSheet("color:#9a9aa5; font-family:Consolas;"
                                " font-size:8pt;")
            form.addRow(label, edit)
            editors.append((entry["index"], edit))
        self._lay.addWidget(form_host)

        def apply_paths():
            if self._asset_apply_cb is None:
                return
            changes = {index: edit.text() for index, edit in editors
                       if edit.text() != edit.property("orig_text")}
            if changes:
                self._asset_apply_cb(changes)

        def revert_paths():
            for _index, edit in editors:
                edit.setText(edit.property("orig_text"))

        btn_row = QtWidgets.QHBoxLayout()
        btn_apply = QtWidgets.QPushButton("Apply path changes")
        btn_apply.setStyleSheet(
            "QPushButton{background:#5a4220;color:#ffe6b0;padding:5px;"
            "font-weight:bold;}QPushButton:hover{background:#76572a;}")
        btn_apply.clicked.connect(apply_paths)
        btn_revert = QtWidgets.QPushButton("Revert paths")
        btn_revert.clicked.connect(revert_paths)
        btn_row.addWidget(btn_apply)
        btn_row.addWidget(btn_revert)
        self._lay.addLayout(btn_row)


# ----------------------------------------------------------- shader conversion

class ShaderExportWorker(QtCore.QObject):
    """Run the external DXBC -> SPIR-V -> GLSL pipeline off the GUI thread."""

    finished = QtCore.Signal(object)

    def __init__(self, path, blobs, data, out_dir):
        super().__init__()
        self.path = path
        self.blobs = blobs
        self.data = data
        self.out_dir = out_dir

    @QtCore.Slot()
    def run(self):
        try:
            result = vfxb_decode.export_shaders(
                self.path, self.blobs, self.out_dir,
                do_glsl=True, keep_spv=False, data=self.data)
        except Exception as exc:  # noqa: BLE001 - cross thread as plain data
            result = {"error": f"{type(exc).__name__}: {exc}"}
        self.finished.emit(result)


# ----------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, start_path=None):
        super().__init__()
        self.setWindowTitle("FF16 VFX Graph Viewer")
        self.resize(1500, 900)
        self._current_dir = None
        self._current_file = None
        self._buf = None            # mutable bytearray of the file being edited
        self._dirty = False         # unsaved in-memory edits
        self._decoded = None        # last decode() result (for constants)
        self._sel_offset = None     # item offset of the selected node (reselect)
        self._shader_result = None  # on-demand export for the current file
        self._shader_thread = None
        self._shader_worker = None
        self._shader_job_path = None

        self._settings = QtCore.QSettings("FF16Tools", "VfxbViewer")

        # --- menu bar
        menu = self.menuBar().addMenu("&File")
        act_open_file = menu.addAction("Open File…")
        act_open_file.triggered.connect(self.open_file_dialog)
        act_open_dir = menu.addAction("Open Folder…")
        act_open_dir.triggered.connect(self.open_dir_dialog)
        menu.addSeparator()
        self._act_save = menu.addAction("Save")
        self._act_save.setShortcut("Ctrl+S")
        self._act_save.triggered.connect(self._save_current)
        self._act_save_pack = menu.addAction("Save and Pack")
        self._act_save_pack.setShortcut("Ctrl+Shift+S")
        self._act_save_pack.triggered.connect(self._save_and_pack)
        self._act_restore = menu.addAction("Restore original (*_origin.vfxb)")
        self._act_restore.triggered.connect(self._restore_current)
        menu.addSeparator()
        self._recent_menu = menu.addMenu("Recent")
        menu.addSeparator()
        act_clear_recent = menu.addAction("Clear Recent")
        act_clear_recent.triggered.connect(self._clear_recent)
        self._rebuild_recent_menu()

        # --- left: file list + buttons
        settings_menu = self.menuBar().addMenu("&Settings")
        settings_menu.addAction("Paths…").triggered.connect(self._show_path_settings)

        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(6, 6, 6, 6)
        btn_row = QtWidgets.QHBoxLayout()
        b_file = QtWidgets.QPushButton("Open File…")
        b_dir = QtWidgets.QPushButton("Open Folder…")
        b_file.clicked.connect(self.open_file_dialog)
        b_dir.clicked.connect(self.open_dir_dialog)
        btn_row.addWidget(b_file)
        btn_row.addWidget(b_dir)
        lv.addLayout(btn_row)
        self.file_list = QtWidgets.QListWidget()
        self.file_list.currentItemChanged.connect(self._file_selected)
        lv.addWidget(self.file_list)

        self.b_constants = QtWidgets.QPushButton("Edit shader constants…")
        self.b_constants.setStyleSheet(
            "QPushButton{background:#3a2e5a;color:#e0d0ff;}"
            "QPushButton:hover{background:#4a3a70;}")
        self.b_constants.clicked.connect(self._show_constants)
        self.b_constants.setEnabled(False)
        lv.addWidget(self.b_constants)

        self.b_assets = QtWidgets.QPushButton("Edit texture / model paths…")
        self.b_assets.setStyleSheet(
            "QPushButton{background:#5a4220;color:#ffe6b0;}"
            "QPushButton:hover{background:#76572a;}")
        self.b_assets.clicked.connect(self._show_assets)
        self.b_assets.setEnabled(False)
        lv.addWidget(self.b_assets)

        self.b_shaders = QtWidgets.QPushButton("Convert shaders to GLSL…")
        self.b_shaders.setStyleSheet(
            "QPushButton{background:#243f55;color:#cfe8ff;font-weight:bold;}"
            "QPushButton:hover{background:#30536f;}"
        )
        self.b_shaders.setToolTip(
            "Extract the embedded TEC shaders and convert them on demand. "
            "This is intentionally not run while loading because it is slow.")
        self.b_shaders.clicked.connect(self._convert_shaders)
        self.b_shaders.setEnabled(False)
        lv.addWidget(self.b_shaders)

        save_row = QtWidgets.QHBoxLayout()
        self.b_save = QtWidgets.QPushButton("Save")
        self.b_save.setStyleSheet(
            "QPushButton{background:#204a5a;color:#d0f0ff;font-weight:bold;}"
            "QPushButton:hover{background:#286078;}")
        self.b_save.clicked.connect(self._save_current)
        self.b_save.setToolTip("Save the VFXB in place without packing a PAC (Ctrl+S).")
        self.b_save_pack = QtWidgets.QPushButton("Save and Pack")
        self.b_save_pack.setToolTip("Save the VFXB, then build the configured PAC (Ctrl+Shift+S).")
        self.b_save_pack.clicked.connect(self._save_and_pack)
        self.b_restore = QtWidgets.QPushButton("Restore original")
        self.b_restore.clicked.connect(self._restore_current)
        save_row.addWidget(self.b_save)
        save_row.addWidget(self.b_save_pack)
        lv.addLayout(save_row)
        lv.addWidget(self.b_restore)

        self.summary = QtWidgets.QLabel("")
        self.summary.setStyleSheet("color:#b9b9c0; padding:4px;")
        self.summary.setWordWrap(True)
        lv.addWidget(self.summary)

        # --- center: graph
        self.graph = GraphView()

        # --- right: inspector
        self.inspector = Inspector()
        self.inspector.set_callbacks(self._apply_patches, self._reselect,
                                     self._apply_asset_changes)

        split = QtWidgets.QSplitter()
        split.addWidget(left)
        split.addWidget(self.graph)
        split.addWidget(self.inspector)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([260, 900, 340])
        self.setCentralWidget(split)

        self.statusBar().showMessage("Open a .vfxb file or a folder to begin.")

        if start_path:
            p = Path(start_path)
            if p.is_dir():
                self.load_dir(p)
            elif p.is_file():
                self.load_dir(p.parent, select=p.name)
        else:
            self._restore_last_session()

    # --- recent files/folders ---------------------------------------------

    def _show_path_settings(self):
        try:
            dialog = PathSettingsDialog(self)
        except (OSError, ValueError) as exc:
            QtWidgets.QMessageBox.warning(self, "Cannot read settings", str(exc))
            return
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            self.statusBar().showMessage("Path settings saved; they apply to the next operation.")

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
        if not self._confirm_discard():
            return
        p = Path(entry)
        if p.is_dir():
            self.load_dir(p)
        elif p.is_file():
            self.load_dir(p.parent, select=p.name)
        else:
            self.statusBar().showMessage(f"No longer exists: {entry}")

    def _restore_last_session(self):
        last_file = self._settings.value("last_file", "", type=str)
        last_dir = self._settings.value("last_dir", "", type=str)
        if last_file and Path(last_file).is_file():
            p = Path(last_file)
            self.load_dir(p.parent, select=p.name)
        elif last_dir and Path(last_dir).is_dir():
            self.load_dir(Path(last_dir))

    # --- file handling ----------------------------------------------------

    def open_file_dialog(self):
        if not self._confirm_discard():
            return
        start = self._current_dir or self._settings.value(
            "last_browse_dir", "", type=str)
        fn, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open VFXB", str(start),
            "VFX Binary (*.vfxb);;All files (*)")
        if fn:
            p = Path(fn)
            self._settings.setValue("last_browse_dir", str(p.parent))
            self.load_dir(p.parent, select=p.name)

    def open_dir_dialog(self):
        if not self._confirm_discard():
            return
        start = self._current_dir or self._settings.value(
            "last_browse_dir", "", type=str)
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Open folder", str(start))
        if d:
            self._settings.setValue("last_browse_dir", d)
            self.load_dir(Path(d))

    def load_dir(self, directory, select=None):
        self._current_dir = directory
        self.file_list.blockSignals(True)
        self.file_list.clear()
        files = sorted(directory.glob("*.vfxb"))
        for f in files:
            self.file_list.addItem(f.name)
        self.file_list.blockSignals(False)

        self._settings.setValue("last_dir", str(directory))
        self._add_recent(directory)

        if not files:
            self.statusBar().showMessage(f"No .vfxb files in {directory}")
            return
        target = select if select in [f.name for f in files] else files[0].name
        items = self.file_list.findItems(target, QtCore.Qt.MatchExactly)
        if items:
            self.file_list.setCurrentItem(items[0])

    def _file_selected(self, cur, _prev):
        if cur is None or self._current_dir is None:
            return
        if not self._confirm_discard():
            # revert selection to the current file without reloading
            self.file_list.blockSignals(True)
            items = self.file_list.findItems(
                self._current_file.name if self._current_file else "",
                QtCore.Qt.MatchExactly)
            if items:
                self.file_list.setCurrentItem(items[0])
            self.file_list.blockSignals(False)
            return
        self.load_file(self._current_dir / cur.text())

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

    def load_file(self, path):
        try:
            with open(path, "rb") as fh:
                self._buf = bytearray(fh.read())
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"Read failed: {e}")
            QtWidgets.QMessageBox.warning(self, "Read failed", str(e))
            self._buf = None
            return
        self._current_file = path
        self._settings.setValue("last_file", str(path))
        self._add_recent(path)
        self._dirty = False
        self._sel_offset = None
        self._shader_result = None
        self._render_from_buf()

    def _render_from_buf(self):
        """(Re)decode the in-memory buffer and rebuild graph + summary."""
        path = self._current_file
        try:
            # skip shader export on re-decode: it's slow and irrelevant to edits
            data = vfxb_decode.decode(str(path), export_shaders_opt=False,
                                      data=bytes(self._buf))
        except Exception as e:  # noqa: BLE001 - surface any decode failure
            self.statusBar().showMessage(f"Decode failed: {e}")
            QtWidgets.QMessageBox.warning(self, "Decode failed", str(e))
            return
        if self._shader_result:
            data["shader_blob"].update(self._shader_result)
            vfxb_decode._annotate_nodes(
                data["items"], self._shader_result.get("programs"))
        self._decoded = data

        roots = [GraphNode(it, 0) for it in data["items"] if "hash" in it]
        self.graph.build(roots, self._on_node_click)
        self.inspector.show_placeholder()
        self._reselect(self._sel_offset)

        st = data["tree_stats"]
        dirty_mark = "  •unsaved" if self._dirty else ""
        has_bak = vfxb_encode.has_backup(path)
        self.summary.setText(
            f"<b>{path.name}</b>{dirty_mark}<br>"
            f"nodes: {st['node_count']}  ·  depth: {st['max_depth']}<br>"
            f"keyframes: {st['total_keyframes']}<br>"
            f"models: {len(data['referenced_models'])}  ·  "
            f"textures: {len(data['referenced_textures'])}  ·  "
            f"model-bound nodes: {st.get('model_binding_count', 0)}<br>"
            f"constants: {len(data['constants'])}  ·  "
            f"prop types: {len(st['property_type_counts'])}"
            + ("<br><span style='color:#7a9'>backup: *_origin.vfxb exists</span>"
               if has_bak else "")
        )
        self.b_restore.setEnabled(has_bak)
        self._act_restore.setEnabled(has_bak)
        self.b_constants.setEnabled(bool(data["constants"]))
        self.b_assets.setEnabled(any(
            entry.get("kind") in ("texture", "model")
            for entry in data.get("string_entries", [])))
        shader_blob = data.get("shader_blob", {})
        can_convert = (shader_blob.get("magic") == "TEC"
                       and shader_blob.get("size", 0) > 0)
        if self._shader_thread is None:
            self.b_shaders.setEnabled(can_convert)
            self.b_shaders.setText(
                "Reconvert shaders to GLSL…" if self._shader_result
                else "Convert shaders to GLSL…")
        self._update_title()
        self.statusBar().showMessage(
            f"{path.name}  -  {st['node_count']} nodes, "
            f"{st['total_keyframes']} keyframes  |  "
            f"click a node to edit · Apply · Save (Ctrl+S)")

    def _update_title(self):
        name = self._current_file.name if self._current_file else "—"
        star = " *" if self._dirty else ""
        self.setWindowTitle(f"FF16 VFX Graph Viewer — {name}{star}")

    # --- node selection / editing -----------------------------------------

    def _on_node_click(self, data):
        # remember the item's byte offset so we can reselect after a rebuild
        self._sel_offset = data.get("offset")
        self.inspector.show_node(data)

    def _reselect(self, offset):
        """Reopen the node whose item offset == `offset` after a rebuild."""
        if offset is None:
            return
        for it in self.graph.scene().items():
            if isinstance(it, NodeItem) and it.gnode.data.get("offset") == offset:
                it.setSelected(True)
                self.inspector.show_node(it.gnode.data)
                break

    def _apply_patches(self, patches):
        """patches: list of (offset, fmt, value). Patch the buffer in place."""
        if self._buf is None:
            return
        try:
            for offset, fmt, value in patches:
                vfxb_encode.patch_scalar(self._buf, offset, fmt, value)
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Patch failed", str(e))
            return
        self._dirty = True
        self._render_from_buf()
        self.statusBar().showMessage(
            f"Applied {len(patches)} change(s) — not yet saved (Ctrl+S)")

    def _show_constants(self):
        """Show the file-global shader constants as an editable panel."""
        if not self._decoded:
            return
        sections = vfxb_encode.build_constant_fields(self._decoded["constants"])
        self._sel_offset = None
        self.inspector.show_constants(sections)

    def _show_assets(self):
        """Show editable global .tex/.mdl entries from the string blob."""
        if not self._decoded:
            return
        entries = [entry for entry in self._decoded.get("string_entries", [])
                   if entry.get("kind") in ("texture", "model")]
        self._sel_offset = None
        self.inspector.show_assets(entries)

    def _convert_shaders(self):
        """Start the explicitly requested shader export without freezing UI."""
        if (self._decoded is None or self._current_file is None
                or self._buf is None or self._shader_thread is not None):
            return

        self.b_shaders.setEnabled(False)
        self.b_shaders.setText("Converting shaders…")
        self.statusBar().showMessage(
            f"Extracting and converting shaders from {self._current_file.name}…")

        self._shader_job_path = Path(self._current_file)
        thread = QtCore.QThread(self)
        worker = ShaderExportWorker(
            str(self._current_file),
            dict(self._decoded["blob_pointers"]),
            bytes(self._buf),
            str(self._current_file.parent),
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._shader_conversion_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._shader_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._shader_thread = thread
        self._shader_worker = worker
        thread.start()

    @QtCore.Slot(object)
    def _shader_conversion_finished(self, result):
        """Attach converted programs if the user still has that file open."""
        job_path = self._shader_job_path
        if job_path != self._current_file:
            return

        programs = result.get("programs") or []
        # Shared stages occur in multiple programs; report unique source files.
        glsl_count = len({
            entry["glsl"] for program in programs
            for stage in ("vertex", "pixel", "unknown")
            for entry in program.get(stage, []) if entry.get("glsl")
        })

        if programs:
            self._shader_result = result
            self._render_from_buf()
            message = (f"Converted {glsl_count} GLSL shader(s) in "
                       f"{len(programs)} program(s). Select an ordered particle "
                       "node, then click ‘View GLSL source…’.")
            if result.get("error"):
                message += f" Warning: {result['error']}"
            self.statusBar().showMessage(message)
            if glsl_count == 0:
                QtWidgets.QMessageBox.warning(
                    self, "GLSL conversion failed",
                    result.get("error", "No GLSL source files were produced."))
        else:
            error = result.get("error", "no shader programs were extracted")
            QtWidgets.QMessageBox.warning(self, "Shader conversion failed", error)
            self.statusBar().showMessage(f"Shader conversion failed: {error}")

    @QtCore.Slot()
    def _shader_thread_finished(self):
        self._shader_thread = None
        self._shader_worker = None
        self._shader_job_path = None
        shader_blob = (self._decoded or {}).get("shader_blob", {})
        can_convert = (shader_blob.get("magic") == "TEC"
                       and shader_blob.get("size", 0) > 0)
        self.b_shaders.setEnabled(can_convert)
        self.b_shaders.setText(
            "Reconvert shaders to GLSL…" if self._shader_result
            else "Convert shaders to GLSL…")

    def _apply_asset_changes(self, replacements):
        """Rebuild the string blob and re-decode after path edits."""
        if self._buf is None or self._decoded is None:
            return
        try:
            delta = vfxb_encode.patch_asset_strings(
                self._buf, self._decoded, replacements)
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Path change failed", str(e))
            return
        self._dirty = True
        self._render_from_buf()
        growth = f"; file grew by {delta} bytes" if delta else ""
        self.statusBar().showMessage(
            f"Applied {len(replacements)} asset path change(s){growth} — "
            "not yet saved (Ctrl+S)")

    # --- save / restore / pack --------------------------------------------

    def _save_current(self):
        if self._buf is None or self._current_file is None:
            return False
        if not self._dirty:
            self.statusBar().showMessage("No changes to save.")
            return True
        try:
            bak = vfxb_encode.ensure_backup(self._current_file)
            with open(self._current_file, "wb") as fh:
                fh.write(self._buf)
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Save failed", str(e))
            return False
        self._dirty = False
        self._render_from_buf()
        self.statusBar().showMessage(
            f"Saved {self._current_file.name}  (original backed up as "
            f"{Path(bak).name})")
        return True

    def _save_and_pack(self):
        if not self._save_current():
            return
        # Packing also works when the VFXB has no new edits.
        self.statusBar().showMessage("VFXB saved · packing…")
        ok, msg = vfxb_encode.pack_diff(self._current_file)
        if not ok:
            QtWidgets.QMessageBox.warning(self, "Pack", msg)
            self.statusBar().showMessage(f"Saved — {msg}")
        else:
            self.statusBar().showMessage(msg)

    def _restore_current(self):
        if self._current_file is None:
            return
        if not vfxb_encode.has_backup(self._current_file):
            QtWidgets.QMessageBox.information(
                self, "Restore",
                "No backup (*_origin.vfxb) exists for this file.")
            return
        resp = QtWidgets.QMessageBox.question(
            self, "Restore original",
            f"Restore {self._current_file.name} from its "
            f"{vfxb_encode.origin_path(self._current_file).name} backup?\n\n"
            "This overwrites the current file with the original and "
            "discards all edits.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if resp != QtWidgets.QMessageBox.Yes:
            return
        try:
            vfxb_encode.restore_backup(self._current_file)
            with open(self._current_file, "rb") as fh:
                self._buf = bytearray(fh.read())
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Restore failed", str(e))
            return
        self._dirty = False
        self._sel_offset = None
        self._shader_result = None
        self._render_from_buf()
        self.statusBar().showMessage(
            f"Restored {self._current_file.name} from backup.")

    def closeEvent(self, event):
        if self._shader_thread is not None:
            QtWidgets.QMessageBox.information(
                self, "Shader conversion in progress",
                "Please wait for shader conversion to finish before closing "
                "the viewer.")
            event.ignore()
            return
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()


def apply_dark_theme(app):
    """Force a dark ("black skin") palette on the whole app.

    The graph canvas paints its own dark background, but the window chrome,
    file list and inspector otherwise inherit the OS theme - which on a light
    desktop leaves the colourful inspector text unreadable on white. A dark
    palette makes every widget dark so the coloured text reads correctly.
    """
    pal = QtGui.QPalette()
    pal.setColor(QtGui.QPalette.Window, QtGui.QColor("#1b1b1f"))
    pal.setColor(QtGui.QPalette.WindowText, QtGui.QColor("#e2e2e8"))
    pal.setColor(QtGui.QPalette.Base, QtGui.QColor("#232328"))
    pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor("#2a2a30"))
    pal.setColor(QtGui.QPalette.Text, QtGui.QColor("#e2e2e8"))
    pal.setColor(QtGui.QPalette.Button, QtGui.QColor("#2a2a30"))
    pal.setColor(QtGui.QPalette.ButtonText, QtGui.QColor("#e2e2e8"))
    pal.setColor(QtGui.QPalette.BrightText, QtGui.QColor("#ff6060"))
    pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor("#3b5b78"))
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor("#ffffff"))
    pal.setColor(QtGui.QPalette.ToolTipBase, QtGui.QColor("#2a2a30"))
    pal.setColor(QtGui.QPalette.ToolTipText, QtGui.QColor("#e2e2e8"))
    pal.setColor(QtGui.QPalette.PlaceholderText, QtGui.QColor("#8a8a92"))
    disabled = QtGui.QColor("#6a6a72")
    for role in (QtGui.QPalette.WindowText, QtGui.QPalette.Text,
                 QtGui.QPalette.ButtonText):
        pal.setColor(QtGui.QPalette.Disabled, role, disabled)
    app.setPalette(pal)
    app.setStyleSheet(
        "QToolTip { color:#e2e2e8; background:#2a2a30; border:1px solid #3a3a42; }"
        "QScrollArea, QListWidget { background:#232328; border:1px solid #33333a; }"
        "QListWidget::item:selected { background:#3b5b78; }"
        "QPushButton { background:#2a2a30; border:1px solid #3a3a42;"
        " padding:5px 8px; border-radius:4px; }"
        "QPushButton:hover { background:#343441; }"
        "QStatusBar { color:#b9b9c0; }"
        "QSplitter::handle { background:#33333a; }"
    )


def main(argv):
    app = QtWidgets.QApplication(argv)
    app.setStyle("Fusion")
    apply_dark_theme(app)
    start = argv[1] if len(argv) > 1 else None
    win = MainWindow(start)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
