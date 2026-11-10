"""Waveform editor (T12, D01): edit the take by hand, Audacity-style, without leaving the app.

* Waveform of the *edited* take (what will be rendered), with an overview strip, a time ruler,
  zoom (Ctrl+wheel, Ctrl+1 / Ctrl+3, Ctrl+F fit, Ctrl+E selection) and scroll (wheel, scrollbar).
* Click to place the cursor, drag (or Shift+click) to select, double-click selects a phrase.
* Space plays from the cursor (or the selection) and stops.
* Cut, Copy, Paste, Delete, Silence (Ctrl+L), Insert silence, Fade in / out, markers (M), Undo / Redo.
* The automatic features as buttons, on the selection or (nothing selected) the whole take:
  Auto-edit pauses, Remove plane noise, Normalise.
* Cuts the app made show where they are: ▼ at each join (Listen / Restore); flub proposals and
  "kept anyway" cuts as shaded regions to Accept / Undo / Cut after all.
* Open in Audacity (exports the edited take) and Bring back from Audacity, for anything heavier.

Every change is an entry in the take's edits.json (saved as she goes). The audio files are never
written to: raw.flac and clean.wav stay exactly as recorded.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QKeySequence, QPainter, QPen, QPolygonF, QShortcut
from PyQt6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QMenu, QMessageBox,
                             QProgressBar, QPushButton, QScrollBar, QSizePolicy, QVBoxLayout, QWidget)

from core.edl import EditList
from core.editing import Editor, compute_auto_pauses, compute_normalise, compute_plane_cuts
from core.renderer import fmt_mmss

from . import theme

BLOCK = 256                      # waveform peaks are kept per 256 samples (≈ 5 ms)
RULER_H = 24
FLAG_BAND = 18                   # top band of the wave area: cut ▼, proposal labels


def fmt_t(s: float, fine: bool = True) -> str:
    s = max(0.0, s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    secs = f"{sec:04.1f}" if fine else f"{int(sec):02d}"
    return f"{int(h)}:{int(m):02d}:{secs}" if h >= 1 else f"{int(m):02d}:{secs}"


def find_audacity() -> str | None:
    exe = shutil.which("audacity")
    if exe:
        return exe
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        for sub in ("Audacity/Audacity.exe", "Programs/Audacity/Audacity.exe"):
            p = Path(base) / sub
            if p.exists():
                return str(p)
    return None


class Job(QThread):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as e:
            self.failed.emit(f"{e}\n\n{traceback.format_exc()}")


# ===================================================================== playback

class Player:
    """Plays the edited take straight from the timeline (rendered block by block as it plays)."""

    def __init__(self, sr: int):
        self.sr = sr
        self.stream = None
        self.pos = 0
        self.start_pos = 0
        self.end = 0
        self.error = ""

    @property
    def playing(self) -> bool:
        return self.stream is not None and self.stream.active

    def play(self, render_block, a: int, b: int) -> bool:
        """render_block(a, b) → float32 samples. Plays output samples [a, b)."""
        self.stop()
        try:
            import sounddevice as sd
        except Exception as e:
            self.error = f"No sound output: {e}"
            return False
        self.pos = self.start_pos = a
        self.end = b

        def cb(outdata, frames, _t, _status):
            n = max(0, min(frames, self.end - self.pos))
            if n:
                outdata[:n, 0] = render_block(self.pos, self.pos + n)
            outdata[n:, 0] = 0
            self.pos += n
            if n < frames:
                raise sd.CallbackStop

        try:
            self.stream = sd.OutputStream(samplerate=self.sr, channels=1, dtype="float32", callback=cb,
                                          blocksize=2048, latency="high")
            self.stream.start()
        except Exception as e:
            self.stream = None
            self.error = f"Couldn't play: {e}"
            return False
        return True

    def play_array(self, x: np.ndarray) -> bool:
        return self.play(lambda a, b: x[a:b], 0, len(x))

    def position(self) -> int:
        """Sample being heard now (allows for the output latency)."""
        lat = int((self.stream.latency if self.stream is not None else 0) * self.sr)
        return max(self.start_pos, self.pos - lat)

    def stop(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
        self.stream = None


# ===================================================================== waveform

class WaveView(QWidget):
    """The zoomable waveform of the edited take."""
    changed = pyqtSignal()                      # cursor / selection / view moved
    item_clicked = pyqtSignal(str, object)      # ("cut" | "marker", CutMark | (pos, Marker))
    context = pyqtSignal(object)                # right-click: global position

    def __init__(self, editor: Editor, parent=None):
        super().__init__(parent)
        self.ed = editor
        self.setMinimumHeight(240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.t0 = 0.0                  # left edge (output seconds)
        self.spp = 1.0                 # seconds per pixel
        self.cursor_t = 0.0
        self.sel: tuple[float, float] | None = None
        self.playhead: float | None = None
        self._anchor: float | None = None
        self._dragging = False
        self._audio_id = None
        self._src_peaks = (np.zeros(0, np.float32), np.zeros(0, np.float32))
        self.peaks = (np.zeros(0, np.float32), np.zeros(0, np.float32))
        self.scale = 1.0
        self._marks: list = []
        self._markers: list = []
        self.refresh()

    # ------------------------------------------------------------------ data

    def refresh(self) -> None:
        """Rebuild the output peaks after an edit (and the source peaks if the audio changed)."""
        ed = self.ed
        if self._audio_id != id(ed.audio):
            x = ed.audio
            n = len(x) // BLOCK * BLOCK
            r = x[:n].reshape(-1, BLOCK)
            lo, hi = r.min(axis=1), r.max(axis=1)
            if len(x) > n:
                lo = np.append(lo, x[n:].min())
                hi = np.append(hi, x[n:].max())
            self._src_peaks = (lo.astype(np.float32), hi.astype(np.float32))
            self._audio_id = id(ed.audio)
        self.peaks = ed.tl.block_peaks(*self._src_peaks, BLOCK)
        top = max(float(np.abs(self.peaks[0]).max(initial=0)), float(self.peaks[1].max(initial=0)), 1e-4)
        self.scale = 0.92 / top
        self._marks = ed.cut_marks()
        self._markers = ed.markers()
        self.clamp_view()
        self.update()

    @property
    def total(self) -> float:
        return self.ed.duration

    def visible_s(self) -> float:
        return max(1, self.width()) * self.spp

    def clamp_view(self) -> None:
        w = max(1, self.width())
        max_spp = max(self.total, 1.0) / w * 1.02
        self.spp = float(min(max(self.spp, 1 / self.ed.sr / 8), max_spp))
        self.t0 = float(min(max(self.t0, 0.0), max(0.0, self.total - self.visible_s() * 0.98)))

    def fit(self) -> None:
        self.spp = max(self.total, 1.0) / max(1, self.width()) * 1.02
        self.t0 = 0.0
        self.clamp_view()
        self.update()
        self.changed.emit()

    def zoom(self, factor: float, about: float | None = None) -> None:
        about = self.t0 + self.visible_s() / 2 if about is None else about
        frac = (about - self.t0) / self.visible_s()
        self.spp /= factor
        self.clamp_view()
        self.t0 = about - frac * self.visible_s()
        self.clamp_view()
        self.update()
        self.changed.emit()

    def show_range(self, a: float, b: float) -> None:
        span = max(b - a, 0.05)
        self.spp = span * 1.1 / max(1, self.width())
        self.clamp_view()
        self.t0 = a - span * 0.05
        self.clamp_view()
        self.update()
        self.changed.emit()

    def ensure_visible(self, t: float) -> None:
        if not (self.t0 <= t <= self.t0 + self.visible_s()):
            self.t0 = t - self.visible_s() * 0.1
            self.clamp_view()
            self.update()
            self.changed.emit()

    def x_of(self, t: float) -> float:
        return (t - self.t0) / self.spp

    def t_of(self, x: float) -> float:
        return min(max(self.t0 + x * self.spp, 0.0), self.total)

    def _pixel_peaks(self, w: int) -> tuple[np.ndarray, np.ndarray, bool]:
        sr = self.ed.sr
        a = self.t0 * sr
        sp = self.spp * sr                                  # samples per pixel
        lo, hi = self.peaks
        if sp >= BLOCK:
            edges = ((a + np.arange(w + 1) * sp) // BLOCK).astype(np.int64)
            edges = np.clip(edges, 0, len(lo))
            ext_lo, ext_hi = np.append(lo, 0).astype(np.float32), np.append(hi, 0).astype(np.float32)
            idx = np.minimum(edges[:-1], len(lo))
            mn, mx = np.minimum.reduceat(ext_lo, idx), np.maximum.reduceat(ext_hi, idx)
            empty = edges[:-1] >= len(lo)
            mn[empty] = 0
            mx[empty] = 0
            return mn, mx, False
        a_i = int(a)
        b_i = int(a + w * sp) + 2
        y = self.ed.tl.render(self.ed.audio, a_i, b_i)
        if not len(y):
            return np.zeros(w, np.float32), np.zeros(w, np.float32), sp < 1
        y = np.append(y, 0).astype(np.float32)
        if sp >= 1:
            idx = np.clip((a + np.arange(w) * sp - a_i).astype(np.int64), 0, len(y) - 1)
            return np.minimum.reduceat(y, idx), np.maximum.reduceat(y, idx), False
        idx = np.clip((a + np.arange(w) * sp - a_i).astype(np.int64), 0, len(y) - 1)
        return y[idx], y[idx], True

    # ------------------------------------------------------------------ paint

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(theme.BG))
        top = RULER_H
        wave_h = h - top - 2
        mid = top + wave_h / 2
        sr = self.ed.sr
        t1 = self.t0 + self.visible_s()

        # shading: digital silence (pauses) and pasted / inserted parts
        sil, ins = QColor(theme.LILAC_SOFT), QColor("#EAF1FB")
        for s, k0, k1 in self.ed.tl.portions(self.t0 * sr, t1 * sr):
            x0, x1 = self.x_of((s.out0 + k0) / sr), self.x_of((s.out0 + k1) / sr)
            if x1 - x0 < 0.5:
                continue
            if s.ins:
                p.fillRect(QRectF(x0, top, x1 - x0, wave_h), ins)
            elif s.kind == "silence":
                p.fillRect(QRectF(x0, top, x1 - x0, wave_h), sil)

        # cuts that are listed but not applied: proposals (amber) and kept-anyway (grey)
        for m in self._marks:
            if m.active:
                continue
            x0, x1 = self.x_of(m.a), self.x_of(m.b)
            if x1 < 0 or x0 > w:
                continue
            col = QColor(theme.AMBER if m.cut.proposed else theme.MUTED)
            col.setAlpha(46 if m.cut.proposed else 30)
            p.fillRect(QRectF(x0, top, x1 - x0, wave_h), col)
            col.setAlpha(255)
            p.fillRect(QRectF(x0, top, x1 - x0, 3), col)
            if x1 - x0 > 60:
                p.setPen(col)
                f = p.font()
                f.setPointSizeF(8.5)
                p.setFont(f)
                label = ("flub? click" if m.cut.reason == "flub" else f"{m.cut.reason}? click") if m.cut.proposed \
                    else f"kept: {m.cut.reason}"
                p.drawText(QRectF(x0 + 4, top + 3, x1 - x0 - 8, FLAG_BAND), Qt.AlignmentFlag.AlignLeft, label)

        # selection
        if self.sel:
            x0, x1 = self.x_of(self.sel[0]), self.x_of(self.sel[1])
            c = QColor(theme.LILAC)
            c.setAlpha(150)
            p.fillRect(QRectF(x0, top, max(1.0, x1 - x0), wave_h), c)

        # waveform
        mn, mx, line = self._pixel_peaks(w)
        amp = wave_h / 2 * self.scale
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawLine(0, int(mid), w, int(mid))
        end_x = min(w, int(self.x_of(self.total)) + 1)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, line)
        if line:
            p.setPen(QPen(QColor(theme.ACCENT), 1.4))
            p.drawPolyline(QPolygonF([QPointF(i, mid - float(mx[i]) * amp) for i in range(end_x)]))
        else:                                   # one vertical line per pixel (fillPath is ~100× slower)
            ys_top = (mid - np.maximum(mx[:end_x], 0) * amp - 0.5).tolist()
            ys_bot = (mid - np.minimum(mn[:end_x], 0) * amp + 0.5).tolist()
            p.setPen(QPen(QColor(theme.ACCENT), 1))
            p.drawLines([QLineF(i + 0.5, t, i + 0.5, b) for i, (t, b) in enumerate(zip(ys_top, ys_bot))])
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # applied cuts: ▼ at the join
        for m in self._marks:
            if not m.active:
                continue
            x = self.x_of(m.a)
            if -8 <= x <= w + 8:
                col = QColor(theme.RED)
                p.setPen(QPen(col, 1, Qt.PenStyle.DotLine))
                p.drawLine(QPointF(x, top), QPointF(x, top + wave_h))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(col)
                p.drawPolygon(QPolygonF([QPointF(x - 6, top + 1), QPointF(x + 6, top + 1), QPointF(x, top + 11)]))

        # ruler + markers
        p.fillRect(0, 0, w, top, QColor(theme.BG))
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawLine(0, top - 1, w, top - 1)
        step = next((s for s in (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)
                     if s / self.spp >= 90), 3600)
        f = p.font()
        f.setPointSizeF(8.5)
        p.setFont(f)
        p.setPen(QColor(theme.MUTED))
        k = int(self.t0 // step)
        while k * step <= t1:
            t = k * step
            x = self.x_of(t)
            if x >= -1:
                p.drawLine(QPointF(x, top - 7), QPointF(x, top - 1))
                p.drawText(QPointF(x + 3, top - 9), fmt_t(t, fine=step < 1))
            k += 1
        for pos, mk in self._markers:
            x = self.x_of(pos)
            if -60 <= x <= w:
                p.setPen(QPen(QColor(theme.ACCENT), 1.5))
                p.drawLine(QPointF(x, 2), QPointF(x, top + wave_h))
                rect = QRectF(x, 2, max(30.0, p.fontMetrics().horizontalAdvance(mk.label) + 10), 15)
                p.fillRect(rect, QColor(theme.ACCENT))
                p.setPen(QColor("white"))
                p.drawText(rect.adjusted(5, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, mk.label)

        # cursor + playhead
        p.setPen(QPen(QColor(theme.TEXT), 1))
        xc = self.x_of(self.cursor_t)
        p.drawLine(QPointF(xc, top), QPointF(xc, top + wave_h))
        if self.playhead is not None:
            p.setPen(QPen(QColor(theme.REC_RED), 2))
            xp = self.x_of(self.playhead)
            p.drawLine(QPointF(xp, 0), QPointF(xp, top + wave_h))
        p.end()

    # ------------------------------------------------------------------ mouse

    def _hit(self, x: float, y: float):
        top = RULER_H
        if y < top:
            for pos, mk in reversed(self._markers):
                if self.x_of(pos) - 3 <= x <= self.x_of(pos) + 60:
                    return "marker", (pos, mk)
            return None
        if y <= top + FLAG_BAND:
            for m in self._marks:
                if m.active and abs(self.x_of(m.a) - x) <= 7:
                    return "cut", m
            for m in self._marks:
                if not m.active and self.x_of(m.a) <= x <= self.x_of(m.b):
                    return "cut", m
        return None

    def mousePressEvent(self, e):
        self.setFocus()
        x, y = e.position().x(), e.position().y()
        if e.button() == Qt.MouseButton.RightButton:
            t = self.t_of(x)
            if not (self.sel and self.sel[0] <= t <= self.sel[1]):
                self.sel = None
                self.cursor_t = t
            self.update()
            self.changed.emit()
            self.context.emit(e.globalPosition().toPoint())
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        hit = self._hit(x, y)
        if hit:
            self.item_clicked.emit(*hit)
            return
        t = self.t_of(x)
        if e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            a = self.sel[0] if self.sel else self.cursor_t
            b = self.sel[1] if self.sel else self.cursor_t
            self.sel = (min(a, t), max(b, t)) if t < a or t > b else (a, t)
            self._anchor = self.sel[0] if t >= self.sel[0] else self.sel[1]
        else:
            self._anchor = t
            self.cursor_t = t
            self.sel = None
        self._dragging = True
        self.update()
        self.changed.emit()

    def mouseMoveEvent(self, e):
        x = e.position().x()
        hit = self._hit(x, e.position().y())
        self.setCursor(Qt.CursorShape.PointingHandCursor if hit else Qt.CursorShape.IBeamCursor)
        if not self._dragging or self._anchor is None:
            return
        if x < 0 or x > self.width():                          # drag past an edge scrolls
            self.t0 += (x if x < 0 else x - self.width()) * self.spp * 0.5
            self.clamp_view()
        t = self.t_of(x)
        if abs(self.x_of(t) - self.x_of(self._anchor)) >= 3:
            self.sel = (min(self._anchor, t), max(self._anchor, t))
            self.cursor_t = self.sel[0]
        self.update()
        self.changed.emit()

    def mouseReleaseEvent(self, _):
        self._dragging = False

    def mouseDoubleClickEvent(self, e):
        """Select the phrase under the mouse (audio between two silences)."""
        tl, sr = self.ed.tl, self.ed.sr
        i = tl.seg_index(self.t_of(e.position().x()) * sr)
        if i < 0:
            return
        vis = tl.vis
        if vis[i].kind == "silence":
            a, b = vis[i].out0, vis[i].end
        else:
            j = i
            while j > 0 and vis[j - 1].kind == "audio":
                j -= 1
            k = i
            while k + 1 < len(vis) and vis[k + 1].kind == "audio":
                k += 1
            a, b = vis[j].out0, vis[k].end
        self.sel = (a / sr, b / sr)
        self.cursor_t = self.sel[0]
        self._dragging = False
        self.update()
        self.changed.emit()

    def wheelEvent(self, e):
        d = e.angleDelta().y() or e.angleDelta().x()
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1.25 if d > 0 else 0.8, about=self.t_of(e.position().x()))
        else:
            self.t0 -= d / 120 * self.visible_s() * 0.12
            self.clamp_view()
            self.update()
            self.changed.emit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.clamp_view()


class Overview(QWidget):
    """The whole take in one strip; the lilac box is what the big view shows. Click to jump."""

    def __init__(self, view: WaveView, parent=None):
        super().__init__(parent)
        self.view = view
        self.setFixedHeight(40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(theme.LILAC_SOFT))
        v = self.view
        lo, hi = v.peaks
        total = max(v.total, 1e-6)
        if len(lo):
            edges = np.minimum((np.arange(w) * len(lo) / w).astype(np.int64), len(lo) - 1)
            mn, mx = np.minimum.reduceat(lo, edges), np.maximum.reduceat(hi, edges)
            mid, amp = h / 2, h / 2 * v.scale
            p.setPen(QPen(QColor(theme.ACCENT), 1))
            tops, bots = (mid - mx * amp).tolist(), (mid - mn * amp).tolist()
            p.drawLines([QLineF(i + 0.5, t, i + 0.5, b) for i, (t, b) in enumerate(zip(tops, bots))])
        x0 = v.t0 / total * w
        x1 = (v.t0 + v.visible_s()) / total * w
        c = QColor(theme.ACCENT)
        c.setAlpha(40)
        p.fillRect(QRectF(x0, 0, max(3.0, x1 - x0), h), c)
        p.setPen(QPen(QColor(theme.ACCENT), 1))
        p.drawRect(QRectF(x0, 0.5, max(3.0, x1 - x0), h - 1))
        p.end()

    def _jump(self, x: float):
        v = self.view
        t = x / max(1, self.width()) * v.total
        v.t0 = t - v.visible_s() / 2
        v.clamp_view()
        v.update()
        v.changed.emit()

    def mousePressEvent(self, e):
        self._jump(e.position().x())

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.MouseButton.LeftButton:
            self._jump(e.position().x())


# ===================================================================== the dialog

class EditorDialog(QDialog):
    """Edit one take. Accept = render it. `editor.audio` is the audio the edits refer to."""

    def __init__(self, take_dir: Path, edl: EditList, audio: np.ndarray, parent=None, auto_cfg=None):
        super().__init__(parent)
        self.setWindowTitle("Edit take")
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(1240, 760)
        self.take_dir = Path(take_dir)
        self.auto_cfg = auto_cfg
        self.editor = Editor(edl, audio)
        self.player = Player(edl.sample_rate)
        self._job: Job | None = None
        self._speech = None                       # VAD of the current audio, reused by the detectors

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 16, 22, 16)
        lay.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("Edit take")
        title.setObjectName("brand")
        head.addWidget(title)
        self.info = QLabel("")
        self.info.setObjectName("muted")
        head.addWidget(self.info, 1)
        self.btn_undo = self._tool("Undo", self.undo, "Undo the last edit (Ctrl+Z)")
        self.btn_redo = self._tool("Redo", self.redo, "Redo (Ctrl+Y)")
        head.addWidget(self.btn_undo)
        head.addWidget(self.btn_redo)
        lay.addLayout(head)

        self.view = WaveView(self.editor)
        self.overview = Overview(self.view)
        lay.addWidget(self.overview)
        lay.addWidget(self.view, 1)
        self.scroll = QScrollBar(Qt.Orientation.Horizontal)
        self.scroll.valueChanged.connect(self._scrolled)
        lay.addWidget(self.scroll)

        # transport + position + zoom
        row = QHBoxLayout()
        self.btn_play = self._tool("▶  Play", self.toggle_play, "Play from the cursor, or the selection (Space)")
        self.btn_play.setObjectName("primary")
        row.addWidget(self.btn_play)
        self.pos = QLabel("")
        self.pos.setMinimumWidth(330)
        row.addWidget(self.pos)
        row.addStretch(1)
        for text, fn, tip in [("−", lambda: self.view.zoom(0.5), "Zoom out (Ctrl+3, Ctrl+wheel)"),
                              ("+", lambda: self.view.zoom(2.0), "Zoom in (Ctrl+1, Ctrl+wheel)"),
                              ("Selection", self.zoom_selection, "Zoom to the selection (Ctrl+E)"),
                              ("Whole take", self.view.fit, "Fit the whole take (Ctrl+F)")]:
            row.addWidget(self._tool(text, fn, tip))
        self.btn_markers = self._tool("Markers", None, "Jump to a marker")
        self.marker_menu = QMenu(self.btn_markers)
        self.btn_markers.setMenu(self.marker_menu)
        row.addWidget(self.btn_markers)
        self.btn_next = self._tool("", self.next_proposal, "Jump to the next proposed cut")
        row.addWidget(self.btn_next)
        lay.addLayout(row)

        # hand edits
        row = QHBoxLayout()
        self.btn_cut = self._tool("Cut", self.do_cut, "Cut the selection (Ctrl+X)")
        self.btn_copy = self._tool("Copy", self.do_copy, "Copy (Ctrl+C)")
        self.btn_paste = self._tool("Paste", self.do_paste, "Paste at the cursor, or over the selection (Ctrl+V)")
        self.btn_delete = self._tool("Delete", self.do_delete, "Delete the selection (Delete)")
        self.btn_silence = self._tool("Silence", self.do_silence, "Make the selection digital silence (Ctrl+L)")
        self.btn_insert = self._tool("Insert silence…", self.do_insert_silence, "Insert seconds of silence at the cursor")
        self.btn_fade_in = self._tool("Fade in", lambda: self.do_fade("in"), "Fade the selection in")
        self.btn_fade_out = self._tool("Fade out", lambda: self.do_fade("out"), "Fade the selection out")
        self.btn_marker = self._tool("Add marker", self.do_marker, "Marker at the cursor (M)")
        for b in (self.btn_cut, self.btn_copy, self.btn_paste, self.btn_delete, self.btn_silence, self.btn_insert,
                  self.btn_fade_in, self.btn_fade_out, self.btn_marker):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

        # automatic features + Audacity
        row = QHBoxLayout()
        self.auto_label = QLabel("")
        self.auto_label.setObjectName("muted")
        row.addWidget(self.auto_label)
        self.btn_auto = self._tool("Auto-edit pauses", self.do_auto_pauses,
                                   "Her pause-marker word → long silence, other pauses → digital silence. "
                                   "Your own silences stay.")
        self.btn_planes = self._tool("Remove plane noise", self.do_planes,
                                     "Find planes and loud noise and cut them at her pauses, like the plane guard")
        self.btn_norm = self._tool("Normalise", self.do_normalise,
                                   "Selection: match the loudness of the rest of the take. "
                                   "Whole take: bring it to the channel loudness (−21 LUFS)")
        for b in (self.btn_auto, self.btn_planes, self.btn_norm):
            row.addWidget(b)
        row.addStretch(1)
        self.btn_aud_open = self._tool("Open in Audacity", self.open_in_audacity,
                                       "Export the edited take and open it in Audacity, for anything heavier")
        self.btn_aud_back = self._tool("Bring back from Audacity…", self.bring_back,
                                       "Use the file you exported from Audacity as the take (you can undo)")
        self.btn_aud_open.setObjectName("link")
        self.btn_aud_back.setObjectName("link")
        row.addWidget(self.btn_aud_open)
        row.addWidget(self.btn_aud_back)
        lay.addLayout(row)

        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.hide()
        lay.addWidget(self.busy)

        foot = QHBoxLayout()
        self.status = QLabel("Click to place the cursor, drag to select. Space plays.")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        foot.addWidget(self.status, 1)
        self.polish = QCheckBox("Studio polish")
        self.polish.setToolTip("Warmth, softer “s” sounds, gentle evening-out (keeps her style)")
        foot.addWidget(self.polish)
        close = QPushButton("Save && close")
        close.clicked.connect(self.reject)
        self.btn_render = QPushButton("Render video")
        self.btn_render.setObjectName("primary")
        self.btn_render.clicked.connect(self.accept)
        foot.addWidget(close)
        foot.addWidget(self.btn_render)
        lay.addLayout(foot)
        for w in (self.polish, close, self.btn_render):     # Space / Enter belong to the waveform
            w.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for b in (close, self.btn_render):
            b.setAutoDefault(False)

        for keys, fn in [("Space", self.toggle_play), ("Ctrl+Z", self.undo), ("Ctrl+Y", self.redo),
                         ("Ctrl+Shift+Z", self.redo), ("Ctrl+X", self.do_cut), ("Ctrl+C", self.do_copy),
                         ("Ctrl+V", self.do_paste), ("Delete", self.do_delete), ("Backspace", self.do_delete),
                         ("Ctrl+L", self.do_silence), ("M", self.do_marker), ("Ctrl+A", self.select_all),
                         ("Ctrl+1", lambda: self.view.zoom(2.0)), ("Ctrl+3", lambda: self.view.zoom(0.5)),
                         ("Ctrl+F", self.view.fit), ("Ctrl+E", self.zoom_selection),
                         ("Home", lambda: self.move_cursor(0.0)), ("End", lambda: self.move_cursor(self.view.total)),
                         ("Escape", self.clear_selection)]:
            sc = QShortcut(QKeySequence(keys), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(fn)

        self.view.changed.connect(self._view_changed)
        self.view.item_clicked.connect(self._item_clicked)
        self.view.context.connect(self._context_menu)
        self.tick = QTimer(self, interval=40, timeout=self._tick)
        self._save_timer = QTimer(self, interval=400, singleShot=True, timeout=self.save_now)
        QTimer.singleShot(0, self.view.fit)
        self.view.setFocus()
        self._after_edit(save=False)

    # ------------------------------------------------------------------ helpers

    def _tool(self, text: str, fn, tip: str = "") -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("tool")
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)      # Space / Delete belong to the waveform
        b.setToolTip(tip)
        if fn is not None:
            b.clicked.connect(fn)
        return b

    @property
    def sel(self):
        return self.view.sel

    def _say(self, text: str) -> None:
        self.status.setText(text)

    def _after_edit(self, save: bool = True, label: str = "") -> None:
        self.player.stop()
        self.view.playhead = None
        self.view.refresh()
        self.overview.update()
        if save:
            self._save_timer.start()            # saved half a second after the last edit (and on close)
        if label:
            self._say(f"{label}. Undo with Ctrl+Z.")
        v = self.view
        pending = [m for m in v._marks if m.cut.proposed]
        self.btn_next.setVisible(bool(pending))
        self.btn_next.setText(f"Next proposed cut ({len(pending)})")
        self.marker_menu.clear()
        for pos, mk in v._markers:
            self.marker_menu.addAction(f"{mk.label}   {fmt_t(pos)}").triggered.connect(
                lambda _=False, pos=pos: self.move_cursor(pos))
        self._view_changed()

    def _view_changed(self) -> None:
        v, ed = self.view, self.editor
        if v.sel:
            a, b = v.sel
            self.pos.setText(f"Selection {fmt_t(a)} – {fmt_t(b)}  ({b - a:.1f} s)")
        else:
            self.pos.setText(f"Cursor {fmt_t(v.cursor_t)}")
        self.info.setText(f"   {fmt_mmss(ed.duration)} edited · {fmt_mmss(ed.edl.duration)} recorded"
                          + ("" if ed.on_original else " · from Audacity"))
        has_sel = bool(v.sel and v.sel[1] - v.sel[0] > 1e-3)
        busy = self._job is not None
        for b in (self.btn_cut, self.btn_copy, self.btn_delete, self.btn_silence, self.btn_fade_in, self.btn_fade_out):
            b.setEnabled(has_sel and not busy)
        self.btn_paste.setEnabled(bool(ed.clipboard) and not busy)
        for b in (self.btn_insert, self.btn_marker, self.btn_auto, self.btn_norm, self.btn_aud_open, self.btn_aud_back,
                  self.btn_render):
            b.setEnabled(not busy)
        self.btn_planes.setEnabled(not busy and ed.on_original)
        self.btn_planes.setToolTip("Find planes and loud noise and cut them at her pauses, like the plane guard"
                                   if ed.on_original else "Not available on audio brought back from Audacity")
        self.btn_undo.setEnabled(ed.can_undo and not busy)
        self.btn_redo.setEnabled(ed.can_redo and not busy)
        self.btn_undo.setToolTip(f"Undo {ed.undo_label().lower()} (Ctrl+Z)" if ed.can_undo else "Nothing to undo")
        self.btn_redo.setToolTip(f"Redo {ed.redo_label().lower()} (Ctrl+Y)" if ed.can_redo else "Nothing to redo")
        self.auto_label.setText("Automatic, on the selection:" if has_sel else "Automatic, on the whole take:")
        self.btn_markers.setEnabled(bool(v._markers) and not busy)
        # scrollbar mirrors the view (milliseconds)
        span = max(0, int((v.total - v.visible_s()) * 1000))
        self.scroll.blockSignals(True)
        self.scroll.setRange(0, span)
        self.scroll.setPageStep(max(1, int(v.visible_s() * 1000)))
        self.scroll.setValue(int(v.t0 * 1000))
        self.scroll.blockSignals(False)
        self.overview.update()

    def _scrolled(self, val: int) -> None:
        self.view.t0 = val / 1000
        self.view.clamp_view()
        self.view.update()
        self.overview.update()

    def move_cursor(self, t: float) -> None:
        self.view.cursor_t = t
        self.view.sel = None
        self.view.ensure_visible(t)
        self.view.update()
        self._view_changed()

    def select(self, a: float, b: float) -> None:
        self.view.sel = (a, b)
        self.view.cursor_t = a
        self.view.ensure_visible(a)
        self.view.update()
        self._view_changed()

    def select_all(self) -> None:
        self.select(0.0, self.view.total)

    def clear_selection(self) -> None:
        if self.view.sel:
            self.view.cursor_t = self.view.sel[0]
        self.view.sel = None
        self.view.update()
        self._view_changed()

    def zoom_selection(self) -> None:
        if self.view.sel:
            self.view.show_range(*self.view.sel)

    # ------------------------------------------------------------------ playback

    def toggle_play(self) -> None:
        if self.player.playing:
            self.stop_play()
            return
        ed, sr = self.editor, self.editor.sr
        if self.view.sel and self.view.sel[1] - self.view.sel[0] > 0.02:
            a, b = self.view.sel
        else:
            a, b = self.view.cursor_t, ed.duration
            if a >= ed.duration - 0.05:
                a = 0.0
        tl, audio = ed.tl, ed.audio
        if self.player.play(lambda i, j: tl.render(audio, i, j), int(a * sr), int(b * sr)):
            self.btn_play.setText("■  Stop")
            self.tick.start()
        else:
            self._say(self.player.error)

    def stop_play(self) -> None:
        self.player.stop()
        self.tick.stop()
        self.view.playhead = None
        self.btn_play.setText("▶  Play")
        self.view.update()

    def _tick(self) -> None:
        if not self.player.playing:
            self.stop_play()
            return
        t = self.player.position() / self.editor.sr
        self.view.playhead = t
        if t > self.view.t0 + self.view.visible_s() or t < self.view.t0:
            self.view.t0 = t - self.view.visible_s() * 0.05
            self.view.clamp_view()
            self._view_changed()
        self.view.update()

    def listen_source(self, a: float, b: float) -> None:
        """Play source audio (e.g. what a cut removed), with a second either side."""
        sr = self.editor.sr
        x = self.editor.audio[max(0, int((a - 1) * sr)): int((b + 1) * sr)]
        self.stop_play()
        if self.player.play_array(np.ascontiguousarray(x)):
            self.tick.stop()
        else:
            self._say(self.player.error)

    # ------------------------------------------------------------------ hand edits

    def _edit(self, label: str, fn) -> None:
        if self._job is not None:
            return
        try:
            fn()
        except Exception as e:
            self._say(f"{label} didn't work: {e}")
            return
        self._after_edit(label=label)

    def do_cut(self):
        if self.sel:
            a, b = self.sel
            self._edit("Cut", lambda: self.editor.cut(a, b))
            self.move_cursor(a)

    def do_copy(self):
        if self.sel:
            self.editor.copy(*self.sel)
            self._say(f"Copied {self.sel[1] - self.sel[0]:.1f} s. Click where it should go and press Paste.")
            self._view_changed()

    def do_paste(self):
        if not self.editor.clipboard:
            return
        at = self.sel[0] if self.sel else self.view.cursor_t
        self._edit("Paste", lambda: self.editor.paste(at, replace=self.sel))
        self.move_cursor(at)

    def do_delete(self):
        if self.sel:
            a, b = self.sel
            self._edit("Delete", lambda: self.editor.delete(a, b))
            self.move_cursor(a)

    def do_silence(self):
        if self.sel:
            self._edit("Silence", lambda: self.editor.silence(*self.sel))

    def do_insert_silence(self):
        secs, ok = QInputDialog.getDouble(self, "Insert silence", "Seconds of silence:", 5.0, 0.1, 600.0, 1)
        if ok:
            at = self.view.cursor_t
            self._edit("Insert silence", lambda: self.editor.insert_silence(at, secs))

    def do_fade(self, kind: str):
        if self.sel:
            self._edit("Fade in" if kind == "in" else "Fade out", lambda: self.editor.fade(*self.sel, kind))

    def do_marker(self):
        at = self.view.playhead if self.view.playhead is not None else self.view.cursor_t
        self._edit("Marker added", lambda: self.editor.add_marker(at))

    def undo(self):
        if self._job is None and self.editor.can_undo:
            label = self.editor.undo()
            self._after_edit()
            self._say(f"Undid: {label.lower()}.")

    def redo(self):
        if self._job is None and self.editor.can_redo:
            label = self.editor.redo()
            self._after_edit()
            self._say(f"Redid: {label.lower()}.")

    # ------------------------------------------------------------------ cuts, proposals, markers

    def next_proposal(self):
        pending = sorted((m for m in self.view._marks if m.cut.proposed), key=lambda m: m.a)
        if not pending:
            return
        after = [m for m in pending if m.a > self.view.cursor_t + 1e-3] or pending
        m = after[0]
        self.view.show_range(m.a - 3, m.b + 3)
        self.select(m.a, m.b)
        self._say("Proposed cut: Space to listen, then click the shaded strip to accept or undo it.")

    def _item_clicked(self, kind: str, obj) -> None:
        menu = QMenu(self)
        if kind == "marker":
            pos, mk = obj
            menu.addAction(f"Go to {mk.label}", lambda: self.move_cursor(pos))
            menu.addAction("Rename…", lambda: self._rename_marker(mk))
            menu.addAction("Delete marker", lambda: self._edit("Marker deleted", lambda: self.editor.remove_marker(mk.id)))
        else:
            m, c = obj, obj.cut
            what = {"plane": "Plane", "flub": "Flub", "noise": "Noise", "manual": "Your"}[c.reason]
            title = menu.addAction(
                f"{what} cut, {c.length:.1f} s" + (" (proposed: said again)" if c.proposed else
                                                    " (kept, not cut)" if c.keep else " removed"))
            title.setEnabled(False)
            menu.addAction("▶  Listen", lambda: self.listen_source(c.start, c.end))
            if c.proposed:
                menu.addAction("Accept cut", lambda: self._edit("Cut accepted", lambda: self.editor.decide_cut(c.id, "accept")))
                menu.addAction("Undo (keep this audio)",
                               lambda: self._edit("Proposal undone", lambda: self.editor.decide_cut(c.id, "undo")))
                menu.addAction("Select it", lambda: self.select(m.a, m.b))
            elif c.keep:
                menu.addAction("Cut it after all", lambda: self._edit("Cut again", lambda: self.editor.decide_cut(c.id, "apply")))
                menu.addAction("Select it", lambda: self.select(m.a, m.b))
            else:
                menu.addAction("Restore (keep this audio)",
                               lambda: self._edit("Cut restored", lambda: self.editor.decide_cut(c.id, "restore")))
        menu.exec(QCursor.pos())

    def _rename_marker(self, mk) -> None:
        text, ok = QInputDialog.getText(self, "Rename marker", "Name:", text=mk.label)
        if ok and text.strip():
            self._edit("Marker renamed", lambda: self.editor.rename_marker(mk.id, text.strip()))

    def _context_menu(self, pos) -> None:
        menu = QMenu(self)
        has_sel = bool(self.sel)
        for text, fn, ok in [("Play", self.toggle_play, True), (None, None, None),
                             ("Cut", self.do_cut, has_sel), ("Copy", self.do_copy, has_sel),
                             ("Paste", self.do_paste, bool(self.editor.clipboard)), ("Delete", self.do_delete, has_sel),
                             ("Silence", self.do_silence, has_sel), ("Insert silence…", self.do_insert_silence, True),
                             (None, None, None), ("Fade in", lambda: self.do_fade("in"), has_sel),
                             ("Fade out", lambda: self.do_fade("out"), has_sel), ("Add marker", self.do_marker, True)]:
            if text is None:
                menu.addSeparator()
            else:
                act = menu.addAction(text, fn)
                act.setEnabled(ok)
        menu.exec(pos)

    # ------------------------------------------------------------------ automatic features

    def _range(self):
        """(output a, b) of the selection, or (None, None) for the whole take; plus source seconds."""
        if self.sel and self.sel[1] - self.sel[0] > 0.05:
            a, b = self.sel
        else:
            a = b = None
        return a, b, self.editor.src_range(a, b)

    def _run(self, text: str, fn, on_done) -> None:
        if self._job is not None:
            return
        self.stop_play()
        self._job = Job(fn, self)
        self._job.done.connect(lambda r: self._job_done(on_done, r))
        self._job.failed.connect(self._job_failed)
        self.busy.show()
        self._say(text)
        self._view_changed()
        self._job.start()

    def _job_done(self, on_done, result) -> None:
        self._job = None
        self.busy.hide()
        try:
            on_done(result)
        except Exception as e:
            self._say(f"That didn't work: {e}")
        self._view_changed()

    def _job_failed(self, msg: str) -> None:
        self._job = None
        self.busy.hide()
        self._say(msg.strip().splitlines()[0][:200])
        self._view_changed()

    def _speech_for(self, audio):
        from core.vad import detect_speech
        if self._speech is None or self._speech[0] is not audio:
            self._speech = (audio, detect_speech(audio, self.editor.sr))
        return self._speech[1]

    def do_auto_pauses(self):
        a, b, (sa, sb) = self._range()
        ed = self.editor
        audio, edl_copy, cfg = ed.audio, EditList.from_json(ed.edl.to_json()), self.auto_cfg

        def work():
            return compute_auto_pauses(audio, edl_copy, cfg, speech=self._speech_for(audio))

        def done(new):
            n = ed.apply_auto_pauses(new, sa, sb)
            marks = sum(p.kind == "marker" for p in new if sa <= p.start and p.end <= sb)
            self._after_edit()
            self._say(f"Auto-edit pauses ({'selection' if a is not None else 'whole take'}): {n} pauses shaped, "
                      f"{marks} pause markers → long silence. Undo with Ctrl+Z.")
        self._run("Finding pauses and her pause markers…", work, done)

    def do_planes(self):
        raw_path = self.take_dir / "raw.flac"
        if not raw_path.exists():
            self._say("This take has no raw recording (raw.flac), so planes can't be found again.")
            return
        a, b, (sa, sb) = self._range()
        ed = self.editor
        audio, edl_copy = ed.audio, EditList.from_json(ed.edl.to_json())

        def work():
            import soundfile as sf
            raw, _ = sf.read(raw_path, dtype="float32", always_2d=False)
            if raw.ndim > 1:
                raw = raw.mean(axis=1)
            if abs(len(raw) - len(audio)) > ed.sr // 2:
                raise ValueError("The raw recording and the cleaned audio don't line up; can't look for planes.")
            return compute_plane_cuts(raw, audio, edl_copy, sa, sb, speech=self._speech_for(audio))

        def done(new):
            n = ed.apply_plane_cuts(new, sa, sb)
            self._after_edit()
            where = "the selection" if a is not None else "the take"
            self._say(f"{n} plane / noise cut{'s' if n != 1 else ''} in {where} (▼ marks each one: click to "
                      f"listen or restore)." if n else f"No planes or loud noise found in {where}.")
        self._run("Listening for planes and loud noise…", work, done)

    def do_normalise(self):
        a, b, _ = self._range()
        ed = self.editor
        tl, audio = ed.tl, ed.audio

        def done(db):
            if abs(db) < 0.05:
                self._say("Already at the right level.")
                return
            ed.apply_normalise(db, a, b)
            self._after_edit()
            self._say(f"Normalise ({'selection' if a is not None else 'whole take'}): {db:+.1f} dB. Undo with Ctrl+Z.")
        self._run("Measuring loudness…", lambda: compute_normalise(tl, audio, a, b), done)

    # ------------------------------------------------------------------ Audacity round trip

    def open_in_audacity(self):
        path = self.take_dir / "for_audacity.wav"
        ed = self.editor
        tl, audio, sr = ed.tl, ed.audio, ed.sr

        def work():
            import soundfile as sf
            sf.write(path, tl.render(audio), sr, subtype="FLOAT")
            return path

        def done(p):
            exe = find_audacity()
            if exe:
                try:
                    subprocess.Popen([exe, str(p)])
                    self._say("Opened in Audacity. When you're done there: File → Export Audio (WAV), "
                              "then Bring back from Audacity.")
                    return
                except OSError:
                    pass
            QMessageBox.information(self, "Audacity", "Audacity isn't installed here (audacityteam.org).\n\n"
                                    f"The edited take is saved as {p.name} in the take's folder; "
                                    "any audio editor can open it. Then use Bring back from Audacity.")
            if sys.platform == "win32":
                subprocess.run(["explorer", "/select,", str(p)])
        self._run("Saving the edited take for Audacity…", work, done)

    def bring_back(self):
        f, _ = QFileDialog.getOpenFileName(self, "The file you exported from Audacity", str(self.take_dir),
                                           "Audio (*.wav *.flac *.mp3 *.m4a *.aiff *.ogg)")
        if not f:
            return
        if QMessageBox.question(self, "Bring back from Audacity",
                                "Use this file as the take from now on? Your edits so far are in it already, "
                                "so the edit list starts fresh (markers are kept). You can undo this.") \
                != QMessageBox.StandardButton.Yes:
            return
        n = 1
        while (self.take_dir / f"audacity_{n}.wav").exists():
            n += 1
        name = f"audacity_{n}.wav"
        dst = self.take_dir / name
        ed = self.editor

        def work():
            import soundfile as sf
            from core.session import load_mono_48k
            x = load_mono_48k(Path(f))
            sf.write(dst, x, ed.sr, subtype="FLOAT")
            return x

        def done(x):
            ed.bring_back(x, name)
            self._after_edit()
            self.view.fit()
            self._say(f"Now editing the Audacity version ({fmt_mmss(len(x) / ed.sr)}). Undo with Ctrl+Z.")
        self._run("Bringing the Audacity file in…", work, done)

    # ------------------------------------------------------------------ save + close

    def save_now(self) -> bool:
        self._save_timer.stop()
        try:
            self.editor.edl.save(self.take_dir / "edits.json")
            return True
        except OSError as e:
            self._say(f"Couldn't save the edits: {e}")
            return False

    def done(self, r):
        if self._job is not None:
            self._job.wait(30000)
        self.stop_play()
        self.save_now()
        super().done(r)
