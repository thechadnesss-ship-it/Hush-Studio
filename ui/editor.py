"""Waveform editor (T12, D01): edit the take by hand without leaving the app, laid out like Audacity
(which Sam knows) in her brand colours.

    File  Edit  Select  View  Transport  Generate  Effect                     ← menu bar
    [⏸][▶][■][⏮][⏭][⟲]   [I]   [✂][⧉][📋][⌶][⎯]  [↶][↷]  [+][−][⊡][↔]   🔊 ▁▂▃▅  ← toolbars + meter
    Automatic: Auto-edit pauses · Remove plane noise · Normalise · Next proposed cut (3)
    ┌─────────────┬────────────────────────────────────────────────────┐
    │             │ 0:00      0:30      1:00      1:30                 │ ← timeline (click = play from there)
    │ Take ▾      │▕ Take name ▼ cut   ▒ flub? ▒                       │ ← clip bar: cuts ▼, proposals
    │ Mono, 48 kHz│ 1.0 ▁▃█▇▅▂  ▁▂█▆▃  ░░░░  ▃▇█▅▁                     │ ← amplitude scale + waveform
    │ [Mute][Sel] │-1.0 ▔▀█▀▔   ▔▀█▀▔   ░░░░  ▔▀█▀▔                    │   (peak + RMS, linear or dB)
    ├─────────────┼────────────────────────────────────────────────────┤
    │ Labels      │     ◉ Marker 1                                      │ ← label track (markers)
    └─────────────┴────────────────────────────────────────────────────┘
    Audio Position [00 h 12 m 03.450 s]   Selection [Start and End ▾] [..] [..]  ← selection toolbar
    Stopped.                                     Studio polish  Save & close  Render video

Audacity's shortcuts work (Space, P, Shift+Space, Home/End, arrows, Ctrl+X/C/V/K/T/L/B/M/Z/Y,
Ctrl+1/2/3/E/F, Ctrl+A, Ctrl+Shift+A, Ctrl+Shift+E). Every change is an entry in the take's
edits.json (saved as she goes); raw.flac and clean.wav are never written to.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QLineF, QPointF, QRectF, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import (QAction, QActionGroup, QColor, QCursor, QIcon, QKeySequence, QPainter, QPainterPath, QPen,
                         QPixmap, QPolygonF)
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel,
                             QLineEdit, QMenu, QMenuBar, QMessageBox, QProgressBar, QPushButton, QScrollBar,
                             QSizePolicy, QToolBar, QToolButton, QVBoxLayout, QWidget)

from core.edl import EditList
from core.editing import Editor, compute_auto_pauses, compute_normalise, compute_plane_cuts
from core.renderer import fmt_mmss

from . import theme

BLOCK = 256                      # waveform peaks are kept per 256 samples (≈ 5 ms)
RULER_H = 28                     # timeline
CLIP_H = 20                      # clip bar (take name, cut ▼, proposals)
LABEL_H = 44                     # label track
GAP = 6                          # between the audio track and the label track
SCALE_W = 46                     # amplitude scale
PANEL_W = 176                    # track control panel
DB_RANGE = 60.0                  # dB waveform: −60 dB at the centre line, like Audacity's default
NORMAL_PX_PER_S = 86.0           # Audacity's "Zoom Normal"
PEAK_COLOR = theme.ACCENT        # her purple; Audacity's darker peak / lighter RMS, in her colours
RMS_COLOR = "#A866C8"
EDITOR_QSS = f"""
QMenuBar {{ background: {theme.BG}; border-bottom: 1px solid {theme.BORDER}; padding: 2px 6px; }}
QMenuBar::item {{ padding: 4px 10px; border-radius: 6px; }}
QMenuBar::item:selected {{ background: {theme.LILAC_SOFT}; color: {theme.ACCENT}; }}
QMenu {{ background: {theme.BG}; border: 1px solid {theme.BORDER}; padding: 4px; }}
QMenu::item {{ padding: 5px 26px 5px 22px; border-radius: 6px; }}
QMenu::item:selected {{ background: {theme.LILAC_SOFT}; color: {theme.ACCENT}; }}
QMenu::item:disabled {{ color: #B9B0C0; }}
QMenu::separator {{ height: 1px; background: {theme.BORDER}; margin: 4px 8px; }}
QFrame#bar {{ background: {theme.BG}; border-bottom: 1px solid {theme.BORDER}; }}
QFrame#bar QLabel, QFrame#bar QToolBar, QFrame#selbar QLabel, QFrame#statusbar QLabel, QFrame#statusbar QCheckBox {{ background: transparent; }}
QFrame#selbar {{ background: {theme.LILAC_SOFT}; border-top: 1px solid {theme.BORDER}; }}
QFrame#statusbar {{ background: {theme.BG}; border-top: 1px solid {theme.BORDER}; }}
QToolBar {{ background: transparent; border: none; spacing: 1px; padding: 0px 4px; }}
QToolBar::separator {{ background: {theme.BORDER}; width: 1px; margin: 4px 6px; }}
QToolButton {{ border: 1px solid transparent; border-radius: 6px; padding: 3px; color: {theme.TEXT}; }}
QToolButton:hover {{ background: {theme.LILAC_SOFT}; border-color: {theme.LILAC}; }}
QToolButton:checked {{ background: {theme.LILAC}; border-color: {theme.LILAC}; }}
QToolButton:disabled {{ color: #B9B0C0; }}
QToolButton#text {{ padding: 4px 10px; font-weight: 500; }}
QFrame#trackpanel {{ background: {theme.LILAC_SOFT}; border-right: 1px solid {theme.BORDER};
    border-bottom: 1px solid {theme.BORDER}; }}
QFrame#labelpanel {{ background: {theme.LILAC_SOFT}; border-right: 1px solid {theme.BORDER}; }}
QFrame#trackpanel QLabel, QFrame#labelpanel QLabel {{ background: transparent; }}
QFrame#trackpanel QToolButton#small, QFrame#labelpanel QToolButton#small {{ background: {theme.BG};
    border: 1px solid {theme.BORDER}; border-radius: 6px; padding: 2px 10px; }}
QFrame#trackpanel QToolButton#small:checked {{ background: {theme.ACCENT}; color: white; border-color: {theme.ACCENT}; }}
QLineEdit#time {{ font-family: Consolas, "Courier New", monospace; font-size: 13px; color: {theme.ACCENT};
    background: {theme.BG}; border: 1px solid {theme.LILAC}; border-radius: 6px; padding: 3px 6px; }}
QLineEdit#bigtime {{ font-family: Consolas, "Courier New", monospace; font-size: 20px; color: {theme.ACCENT};
    background: {theme.BG}; border: 1px solid {theme.LILAC}; border-radius: 8px; padding: 2px 8px; }}
QComboBox#selmode {{ border-radius: 6px; padding: 3px 8px; }}
"""


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


# ===================================================================== icons (drawn here, not Audacity's art)

def make_icon(kind: str, color: str = theme.TEXT, size: int = 24) -> QIcon:
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 24, size / 24)
    c = QColor(color)
    pen = QPen(c, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)

    def fill(*pts):
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))
        p.restore()

    def magnifier():
        p.drawEllipse(QRectF(4, 4, 12, 12))
        p.drawLine(QLineF(14.5, 14.5, 20, 20))

    if kind == "play":
        fill((7, 4.5), (19.5, 12), (7, 19.5))
    elif kind == "pause":
        fill((6.5, 5), (10, 5), (10, 19), (6.5, 19))
        fill((14, 5), (17.5, 5), (17.5, 19), (14, 19))
    elif kind == "stop":
        fill((6, 6), (18, 6), (18, 18), (6, 18))
    elif kind == "skip_start":
        fill((5, 6), (7.5, 6), (7.5, 18), (5, 18))
        fill((19, 6), (9, 12), (19, 18))
    elif kind == "skip_end":
        fill((16.5, 6), (19, 6), (19, 18), (16.5, 18))
        fill((5, 6), (15, 12), (5, 18))
    elif kind == "loop":
        p.drawArc(QRectF(4.5, 6, 15, 12), 100 * 16, 290 * 16)
        fill((10.5, 3), (15, 6), (10.5, 9))
    elif kind == "select":
        p.drawLine(QLineF(12, 5, 12, 19))
        p.drawLine(QLineF(9, 5, 15, 5))
        p.drawLine(QLineF(9, 19, 15, 19))
    elif kind == "cut":
        p.drawEllipse(QRectF(4.5, 14.5, 5.5, 5.5))
        p.drawEllipse(QRectF(14, 14.5, 5.5, 5.5))
        p.drawLine(QLineF(9, 15, 16.5, 4))
        p.drawLine(QLineF(15, 15, 7.5, 4))
    elif kind == "copy":
        p.drawRoundedRect(QRectF(9, 4, 10, 12), 1.5, 1.5)
        p.save()
        p.setBrush(QColor(theme.BG))
        p.drawRoundedRect(QRectF(5, 8, 10, 12), 1.5, 1.5)
        p.restore()
    elif kind == "paste":
        p.drawRoundedRect(QRectF(5.5, 6, 13, 15), 1.5, 1.5)
        fill((9, 4), (15, 4), (15, 8), (9, 8))
        p.drawLine(QLineF(9, 12, 15, 12))
        p.drawLine(QLineF(9, 16, 13, 16))
    elif kind == "trim":
        p.drawPolyline(QPolygonF([QPointF(8, 5), QPointF(5, 5), QPointF(5, 19), QPointF(8, 19)]))
        p.drawPolyline(QPolygonF([QPointF(16, 5), QPointF(19, 5), QPointF(19, 19), QPointF(16, 19)]))
        for x, hh in ((9.5, 3), (12, 6), (14.5, 3.5)):
            p.drawLine(QLineF(x, 12 - hh, x, 12 + hh))
    elif kind == "silence":
        for x, hh in ((4, 3), (6.5, 6), (9, 4)):
            p.drawLine(QLineF(x, 12 - hh, x, 12 + hh))
        p.drawLine(QLineF(11.5, 12, 20, 12))
    elif kind in ("undo", "redo"):
        if kind == "redo":
            p.translate(24, 0)
            p.scale(-1, 1)
        path = QPainterPath(QPointF(19, 18))
        path.quadTo(QPointF(19, 8.5), QPointF(9.5, 9.5))
        p.drawPath(path)
        fill((4.5, 9.5), (10.5, 5), (10.5, 14))
    elif kind == "zoom_in":
        magnifier()
        p.drawLine(QLineF(7, 10, 13, 10))
        p.drawLine(QLineF(10, 7, 10, 13))
    elif kind == "zoom_out":
        magnifier()
        p.drawLine(QLineF(7, 10, 13, 10))
    elif kind == "zoom_sel":
        magnifier()
        p.drawLine(QLineF(7.5, 7.5, 7.5, 12.5))
        p.drawLine(QLineF(12.5, 7.5, 12.5, 12.5))
    elif kind == "zoom_fit":
        magnifier()
        p.drawLine(QLineF(6.5, 10, 13.5, 10))
        fill((6, 10), (8.5, 8), (8.5, 12))
        fill((14, 10), (11.5, 8), (11.5, 12))
    elif kind == "speaker":
        fill((4, 9.5), (8, 9.5), (13, 5), (13, 19), (8, 14.5), (4, 14.5))
        p.drawArc(QRectF(11, 8, 6, 8), -60 * 16, 120 * 16)
        p.drawArc(QRectF(11, 5, 10, 14), -60 * 16, 120 * 16)
    elif kind == "label":
        p.drawEllipse(QRectF(5, 9, 6, 6))
        p.drawRoundedRect(QRectF(11, 7, 9, 10), 2, 2)
    p.end()
    return QIcon(pm)


# ===================================================================== background work

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
        self.loop = False
        self.muted = False
        self.peak_db = -120.0
        self.error = ""

    @property
    def playing(self) -> bool:
        return self.stream is not None and self.stream.active

    def play(self, render_block, a: int, b: int, loop: bool = False) -> bool:
        """render_block(a, b) → float32 samples. Plays output samples [a, b) (round and round if loop)."""
        self.stop()
        try:
            import sounddevice as sd
        except Exception as e:
            self.error = f"No sound output: {e}"
            return False
        self.pos = self.start_pos = a
        self.end = b
        self.loop = loop and b > a

        def cb(outdata, frames, _t, _status):
            filled = 0
            while filled < frames:
                n = min(frames - filled, self.end - self.pos)
                if n <= 0:
                    if self.loop:
                        self.pos = self.start_pos
                        continue
                    break
                outdata[filled:filled + n, 0] = render_block(self.pos, self.pos + n)
                self.pos += n
                filled += n
            outdata[filled:, 0] = 0
            if self.muted:
                outdata[:] = 0
            pk = float(np.abs(outdata).max()) if frames else 0.0
            self.peak_db = 20 * np.log10(pk) if pk > 1e-6 else -120.0
            if filled < frames:
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
        p = self.pos - lat
        if self.loop and p < self.start_pos:
            p = self.end - (self.start_pos - p)
        return max(self.start_pos, p)

    def stop(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
        self.stream = None
        self.peak_db = -120.0


# ===================================================================== small widgets

class PlayMeter(QWidget):
    """Playback meter, −60…0 dB with a scale under it (Audacity's meter toolbar, her colours)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.db = -120.0
        self.peak = -120.0
        self.setFixedSize(230, 30)
        self.setToolTip("Playback level")

    def set_db(self, db: float) -> None:
        self.db = db
        self.peak = max(db, self.peak - 0.8)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        w = self.width()
        bar = QRectF(0, 3, w, 10)
        p.fillRect(bar, QColor(theme.LILAC_SOFT))
        frac = lambda d: min(max((d + 60) / 60, 0.0), 1.0)
        col = QColor(theme.RED if self.peak > -0.5 else theme.ACCENT)
        p.fillRect(QRectF(0, 3, w * frac(self.db), 10), col)
        if self.peak > -60:
            p.fillRect(QRectF(w * frac(self.peak) - 1.5, 3, 2, 10), QColor(theme.AMBER if self.peak > -6 else theme.ACCENT))
        f = p.font()
        f.setPointSizeF(7)
        p.setFont(f)
        p.setPen(QColor(theme.MUTED))
        for d in (-60, -48, -36, -24, -12, 0):
            x = w * frac(d)
            p.drawLine(QPointF(x, 14), QPointF(x, 17))
            txt = str(d)
            tw = p.fontMetrics().horizontalAdvance(txt)
            p.drawText(QPointF(min(max(x - tw / 2, 0), w - tw), 27), txt)
        p.end()


class TimeField(QLineEdit):
    """Audacity-style time box, `00 h 02 m 10.500 s`: type over the digits, Enter applies."""
    edited = pyqtSignal(float)

    def __init__(self, big: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("bigtime" if big else "time")
        self.setInputMask(r"99 \h 99 \m 99.999 \s;_")
        self.setFixedWidth(250 if big else 166)
        self.set_seconds(0.0)
        self.editingFinished.connect(lambda: self.edited.emit(self.seconds()))

    def set_seconds(self, s: float) -> None:
        s = max(0.0, s)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if round(sec, 3) >= 60:
            sec, m = 0.0, m + 1
        self.setText(f"{int(h):02d} h {int(m):02d} m {sec:06.3f} s")

    def seconds(self) -> float:
        digits = [c for c in self.text() if c.isdigit() or c == "."]
        t = "".join(digits)
        try:
            return int(t[0:2]) * 3600 + int(t[2:4]) * 60 + float(t[4:])
        except ValueError:
            return 0.0


# ===================================================================== waveform

class WaveView(QWidget):
    """Timeline, clip bar, amplitude scale + waveform of the edited take, and the label track."""
    changed = pyqtSignal()                      # cursor / selection / view moved
    item_clicked = pyqtSignal(str, object)      # ("cut" | "marker", CutMark | (pos, Marker))
    context = pyqtSignal(object)                # right-click on the audio: global position
    quick_play = pyqtSignal(float)              # click on the timeline: play from there
    add_label_at = pyqtSignal(float)            # double-click on the label track

    def __init__(self, editor: Editor, name: str = "Take", parent=None):
        super().__init__(parent)
        self.ed = editor
        self.name = name
        self.setMinimumHeight(300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.t0 = 0.0                  # left edge (output seconds)
        self.spp = 1.0                 # seconds per pixel
        self.cursor_t = 0.0
        self.sel: tuple[float, float] | None = None
        self.playhead: float | None = None
        self.db_view = False
        self.vmax = 1.0                # linear view: ±vmax fills the track
        self._fitted = True            # showing the whole take: stays so when the window resizes
        self._anchor: float | None = None
        self._dragging = False
        self._audio_id = None
        self._src = (np.zeros(0, np.float32),) * 3
        self.peaks = (np.zeros(0, np.float32),) * 3
        self._marks: list = []
        self._markers: list = []
        self.refresh()
        self.reset_vzoom()

    # ------------------------------------------------------------------ data

    def refresh(self) -> None:
        """Rebuild the output peaks after an edit (and the source peaks if the audio changed)."""
        ed = self.ed
        if self._audio_id != id(ed.audio):
            x = ed.audio
            n = len(x) // BLOCK * BLOCK
            r = x[:n].reshape(-1, BLOCK)
            lo, hi = r.min(axis=1), r.max(axis=1)
            ms = np.einsum("ij,ij->i", r, r, dtype=np.float64) / BLOCK
            if len(x) > n:
                tail = x[n:]
                lo, hi = np.append(lo, tail.min()), np.append(hi, tail.max())
                ms = np.append(ms, float(np.mean(tail.astype(np.float64) ** 2)))
            self._src = (lo.astype(np.float32), hi.astype(np.float32), ms.astype(np.float32))
            self._audio_id = id(ed.audio)
        self.peaks = ed.tl.block_peaks(self._src[0], self._src[1], BLOCK, self._src[2])
        self._marks = ed.cut_marks()
        self._markers = ed.markers()
        self.clamp_view()
        self.update()

    def reset_vzoom(self) -> None:
        """±1.0 like Audacity, zoomed in (halving) while her loudest peak would fill under half of it."""
        peak = max(float(np.abs(self.peaks[0]).max(initial=0)), float(self.peaks[1].max(initial=0)))
        v = 1.0
        while v / 2 >= peak * 1.05 and v > 1 / 64:
            v /= 2
        self.vmax = v
        self.update()

    @property
    def total(self) -> float:
        return self.ed.duration

    @property
    def wave_w(self) -> int:
        return max(1, self.width() - SCALE_W)

    def visible_s(self) -> float:
        return self.wave_w * self.spp

    def clamp_view(self) -> None:
        max_spp = max(self.total, 1.0) / self.wave_w * 1.02
        self.spp = float(min(max(self.spp, 1 / self.ed.sr / 8), max_spp))
        self.t0 = float(min(max(self.t0, 0.0), max(0.0, self.total - self.visible_s() * 0.98)))

    def fit(self) -> None:
        self._fitted = True
        self.spp = max(self.total, 1.0) / self.wave_w * 1.02
        self.t0 = 0.0
        self.clamp_view()
        self.update()
        self.changed.emit()

    def zoom(self, factor: float, about: float | None = None) -> None:
        about = self.t0 + self.visible_s() / 2 if about is None else about
        frac = (about - self.t0) / self.visible_s()
        self._fitted = False
        self.spp /= factor
        self.clamp_view()
        self.t0 = about - frac * self.visible_s()
        self.clamp_view()
        self.update()
        self.changed.emit()

    def zoom_normal(self) -> None:
        about = self.cursor_t
        self._fitted = False
        self.spp = 1 / NORMAL_PX_PER_S
        self.clamp_view()
        self.t0 = about - self.visible_s() / 2
        self.clamp_view()
        self.update()
        self.changed.emit()

    def show_range(self, a: float, b: float) -> None:
        span = max(b - a, 0.05)
        self._fitted = False
        self.spp = span * 1.1 / self.wave_w
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
        return SCALE_W + (t - self.t0) / self.spp

    def t_of(self, x: float) -> float:
        return min(max(self.t0 + (x - SCALE_W) * self.spp, 0.0), self.total)

    # geometry
    def _wave_rect(self) -> QRectF:
        top = RULER_H + CLIP_H
        return QRectF(SCALE_W, top, self.wave_w, max(10, self.height() - LABEL_H - GAP - top))

    def _label_top(self) -> float:
        return self.height() - LABEL_H

    def _pixel_peaks(self, w: int):
        """Per-pixel min, max, RMS of the edited take in view; `line` when zoomed to single samples."""
        sr = self.ed.sr
        a = self.t0 * sr
        sp = self.spp * sr                                  # samples per pixel
        lo, hi, ms = self.peaks
        if sp >= BLOCK:
            edges = np.clip(((a + np.arange(w + 1) * sp) // BLOCK).astype(np.int64), 0, len(lo))
            idx = edges[:-1]
            cnt = np.maximum(edges[1:] - edges[:-1], 1)
            ext = lambda v: np.append(v, 0).astype(np.float32)
            mn, mx = np.minimum.reduceat(ext(lo), idx), np.maximum.reduceat(ext(hi), idx)
            rms = np.sqrt(np.add.reduceat(ext(ms).astype(np.float64), idx) / cnt).astype(np.float32)
            empty = idx >= len(lo)
            mn[empty] = mx[empty] = rms[empty] = 0
            return mn, mx, np.minimum(rms, np.maximum(mx, -mn)), False
        a_i = int(a)
        b_i = int(a + w * sp) + 2
        y = self.ed.tl.render(self.ed.audio, a_i, b_i)
        z = np.zeros(w, np.float32)
        if not len(y):
            return z, z, z, sp < 1
        y = np.append(y, 0).astype(np.float32)
        idx = np.clip((a + np.arange(w) * sp - a_i).astype(np.int64), 0, len(y) - 1)
        if sp >= 1:
            nxt = np.append(idx[1:], len(y) - 1)
            cnt = np.maximum(nxt - idx, 1)
            rms = np.sqrt(np.add.reduceat(y.astype(np.float64) ** 2, idx) / cnt).astype(np.float32)
            mn, mx = np.minimum.reduceat(y, idx), np.maximum.reduceat(y, idx)
            return mn, mx, np.minimum(rms, np.maximum(mx, -mn)), False
        return y[idx], y[idx], z, True

    def _y(self, v: np.ndarray, mid: float, half: float) -> np.ndarray:
        """Amplitude → screen y (linear ±vmax, or dB with −60 dB at the centre)."""
        if self.db_view:
            with np.errstate(divide="ignore"):
                d = 20 * np.log10(np.maximum(np.abs(v), 1e-9))
            u = np.sign(v) * np.clip((d + DB_RANGE) / DB_RANGE, 0.0, 1.0)
        else:
            u = np.clip(v / self.vmax, -1.0, 1.0)
        return mid - u * half

    # ------------------------------------------------------------------ paint

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(theme.BG))
        sr = self.ed.sr
        t1 = self.t0 + self.visible_s()
        wr = self._wave_rect()
        top, wave_h = wr.top(), wr.height()
        mid, half = top + wave_h / 2, wave_h / 2 - 1
        lt = self._label_top()
        x_end = min(float(w), self.x_of(self.total))
        small = p.font()
        small.setPointSizeF(8.5)
        p.setFont(small)

        # track background: white inside the clip, lilac-grey after its end (Audacity's empty space)
        p.fillRect(QRectF(SCALE_W, top, w - SCALE_W, wave_h), QColor("#F6F3F8"))
        p.fillRect(QRectF(SCALE_W, top, max(0.0, x_end - SCALE_W), wave_h), QColor(theme.BG))

        # digital silence (pauses) and pasted / inserted parts, faintly
        sil, ins = QColor("#FAF7FC"), QColor("#EAF1FB")
        for s, k0, k1 in self.ed.tl.portions(self.t0 * sr, t1 * sr):
            x0, x1 = self.x_of((s.out0 + k0) / sr), self.x_of((s.out0 + k1) / sr)
            if x1 - x0 < 0.5:
                continue
            if s.ins:
                p.fillRect(QRectF(x0, top, x1 - x0, wave_h), ins)
            elif s.kind == "silence":
                p.fillRect(QRectF(x0, top, x1 - x0, wave_h), sil)

        # selection (behind the waveform, across the track and the label track)
        if self.sel:
            x0, x1 = max(SCALE_W, self.x_of(self.sel[0])), self.x_of(self.sel[1])
            c = QColor(theme.LILAC)
            c.setAlpha(170)
            p.fillRect(QRectF(x0, top, max(1.0, x1 - x0), wave_h), c)
            c.setAlpha(110)
            p.fillRect(QRectF(x0, lt, max(1.0, x1 - x0), LABEL_H), c)

        # cuts listed but not applied: proposals (amber) and kept-anyway (grey)
        for m in self._marks:
            if m.active:
                continue
            x0, x1 = self.x_of(m.a), self.x_of(m.b)
            if x1 < SCALE_W or x0 > w:
                continue
            col = QColor(theme.AMBER if m.cut.proposed else theme.MUTED)
            col.setAlpha(40 if m.cut.proposed else 26)
            p.fillRect(QRectF(x0, top, x1 - x0, wave_h), col)

        # waveform: peak, then RMS inside it
        ww = self.wave_w
        mn, mx, rms, line = self._pixel_peaks(ww)
        n_draw = max(0, min(ww, int(x_end - SCALE_W) + 1))
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawLine(QLineF(SCALE_W, mid, w, mid))
        if line:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            ys = self._y(mx[:n_draw], mid, half).tolist()
            p.setPen(QPen(QColor(PEAK_COLOR), 1.4))
            p.drawPolyline(QPolygonF([QPointF(SCALE_W + i, y) for i, y in enumerate(ys)]))
            if self.spp * sr < 0.25:                        # Audacity's sample dots when zoomed right in
                p.setBrush(QColor(PEAK_COLOR))
                for i in range(0, n_draw, max(1, int(1 / (self.spp * sr)))):
                    p.drawEllipse(QPointF(SCALE_W + i, ys[i]), 2.2, 2.2)
        else:
            yt = self._y(np.maximum(mx[:n_draw], 0), mid, half) - 0.5
            yb = self._y(np.minimum(mn[:n_draw], 0), mid, half) + 0.5
            p.setPen(QPen(QColor(PEAK_COLOR), 1))
            p.drawLines([QLineF(SCALE_W + i + 0.5, a, SCALE_W + i + 0.5, b)
                         for i, (a, b) in enumerate(zip(yt.tolist(), yb.tolist()))])
            rt = self._y(rms[:n_draw], mid, half)
            rb = self._y(-rms[:n_draw], mid, half)
            p.setPen(QPen(QColor(RMS_COLOR), 1))
            p.drawLines([QLineF(SCALE_W + i + 0.5, a, SCALE_W + i + 0.5, b)
                         for i, (a, b) in enumerate(zip(rt.tolist(), rb.tolist())) if b - a >= 1])
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # clip bar: the take as one clip, cut joins ▼, proposals
        cb = QRectF(max(SCALE_W, self.x_of(0)), RULER_H, max(0.0, x_end - max(SCALE_W, self.x_of(0))), CLIP_H)
        p.fillRect(QRectF(SCALE_W, RULER_H, w - SCALE_W, CLIP_H), QColor("#F6F3F8"))
        p.fillRect(cb, QColor(theme.LILAC))
        p.setPen(QColor(theme.TEXT))
        p.drawText(cb.adjusted(8, 0, -4, 0), Qt.AlignmentFlag.AlignVCenter, self.name)
        for m in self._marks:
            x0, x1 = self.x_of(m.a), self.x_of(m.b)
            if m.active:
                if SCALE_W - 8 <= x0 <= w + 8:
                    col = QColor(theme.RED)
                    p.setPen(QPen(col, 1, Qt.PenStyle.DotLine))
                    p.drawLine(QLineF(x0, RULER_H + CLIP_H, x0, top + wave_h))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(col)
                    p.drawPolygon(QPolygonF([QPointF(x0 - 6, RULER_H + 3), QPointF(x0 + 6, RULER_H + 3),
                                             QPointF(x0, RULER_H + CLIP_H - 3)]))
            elif x1 >= SCALE_W and x0 <= w:
                col = QColor(theme.AMBER if m.cut.proposed else theme.MUTED)
                p.fillRect(QRectF(x0, RULER_H + 2, max(3.0, x1 - x0), CLIP_H - 4), col)
                if x1 - x0 > 46:
                    p.setPen(QColor("white"))
                    label = ("flub?" if m.cut.reason == "flub" else f"{m.cut.reason}?") if m.cut.proposed \
                        else f"kept {m.cut.reason}"
                    p.drawText(QRectF(x0 + 4, RULER_H, x1 - x0 - 6, CLIP_H), Qt.AlignmentFlag.AlignVCenter, label)

        # amplitude scale
        p.fillRect(QRectF(0, RULER_H, SCALE_W, lt - RULER_H), QColor(theme.BG))
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawLine(QLineF(SCALE_W - 0.5, top, SCALE_W - 0.5, top + wave_h))
        p.setPen(QColor(theme.MUTED))
        if self.db_view:
            ticks = [(mid - (d + DB_RANGE) / DB_RANGE * half * s, f"{d:g}") for d in (0, -12, -24, -36, -48)
                     for s in (1, -1)] + [(mid, "-∞")]
        else:
            dec = 1 if self.vmax >= 0.5 else 2 if self.vmax >= 0.05 else 3
            ticks = [(mid - f * half, f"{f * self.vmax:.{dec}f}") for f in (1, 0.5, 0, -0.5, -1)]
        fm = p.fontMetrics()
        for y, txt in ticks:
            p.drawLine(QLineF(SCALE_W - 5, y, SCALE_W - 1, y))
            ty = min(max(y + fm.ascent() / 2 - 1, top + fm.ascent()), top + wave_h - 2)
            p.drawText(QPointF(SCALE_W - 7 - fm.horizontalAdvance(txt), ty), txt)

        # label track
        p.fillRect(QRectF(0, lt - GAP, w, GAP), QColor(theme.LILAC_SOFT))
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawLine(QLineF(0, lt - GAP, w, lt - GAP))
        p.drawLine(QLineF(0, lt, w, lt))
        for pos, mk in self._markers:
            x = self.x_of(pos)
            tw = fm.horizontalAdvance(mk.label) + 14
            if x + tw + 10 < SCALE_W or x > w:
                continue
            p.setPen(QPen(QColor(theme.ACCENT), 1.2))
            p.drawLine(QLineF(x, lt + 4, x, lt + LABEL_H - 4))
            p.setBrush(QColor(theme.ACCENT))
            p.drawEllipse(QPointF(x, lt + LABEL_H / 2), 4, 4)
            box = QRectF(x + 8, lt + LABEL_H / 2 - 10, tw, 20)
            p.setBrush(QColor(theme.BG))
            p.drawRoundedRect(box, 5, 5)
            p.setPen(QColor(theme.TEXT))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, mk.label)

        # timeline ruler
        p.fillRect(QRectF(0, 0, w, RULER_H), QColor(theme.LILAC_SOFT))
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawLine(QLineF(0, RULER_H - 0.5, w, RULER_H - 0.5))
        step = next((s for s in (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)
                     if s / self.spp >= 90), 3600)
        minor = step / 5
        p.setPen(QColor(theme.MUTED))
        k = int(self.t0 // minor)
        while k * minor <= t1:
            t = k * minor
            x = self.x_of(t)
            if x >= SCALE_W:
                major = abs(t / step - round(t / step)) < 1e-6
                p.drawLine(QLineF(x, RULER_H - (9 if major else 4), x, RULER_H - 1))
                if major:
                    p.drawText(QPointF(x + 3, RULER_H - 11), fmt_t(t, fine=step < 1))
            k += 1
        if self.playhead is not None:
            xp = self.x_of(self.playhead)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.REC_RED))
            p.drawPolygon(QPolygonF([QPointF(xp - 6, RULER_H - 12), QPointF(xp + 6, RULER_H - 12),
                                     QPointF(xp, RULER_H - 2)]))

        # cursor + playhead
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        xc = self.x_of(self.cursor_t)
        if xc >= SCALE_W:
            p.setPen(QPen(QColor(theme.TEXT), 1))
            p.drawLine(QLineF(xc, top, xc, h))
        if self.playhead is not None:
            xp = self.x_of(self.playhead)
            if xp >= SCALE_W:
                p.setPen(QPen(QColor(theme.REC_RED), 2))
                p.drawLine(QLineF(xp, RULER_H, xp, h))
        p.end()

    # ------------------------------------------------------------------ mouse

    def _hit(self, x: float, y: float):
        if RULER_H <= y < RULER_H + CLIP_H:
            for m in self._marks:
                if m.active and abs(self.x_of(m.a) - x) <= 7:
                    return "cut", m
            for m in self._marks:
                if not m.active and self.x_of(m.a) - 2 <= x <= self.x_of(m.b) + 2:
                    return "cut", m
        if y >= self._label_top():
            fm = self.fontMetrics()
            for pos, mk in reversed(self._markers):
                x0 = self.x_of(pos)
                if x0 - 6 <= x <= x0 + 8 + fm.horizontalAdvance(mk.label) + 16:
                    return "marker", (pos, mk)
        return None

    def mousePressEvent(self, e):
        self.setFocus()
        x, y = e.position().x(), e.position().y()
        if x < SCALE_W and RULER_H + CLIP_H <= y < self._label_top():
            self._scale_click(e)
            return
        if y < RULER_H:
            if e.button() == Qt.MouseButton.LeftButton:
                self.quick_play.emit(self.t_of(x))
            return
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

    def _scale_click(self, e) -> None:
        """Audacity's vertical ruler: click zooms in, Shift+click zooms out, right-click for the menu."""
        if e.button() == Qt.MouseButton.RightButton:
            m = QMenu(self)
            m.addAction("Zoom In", lambda: self._vzoom(0.5))
            m.addAction("Zoom Out", lambda: self._vzoom(2.0))
            m.addAction("Zoom Reset", self.reset_vzoom)
            m.addSeparator()
            lin = m.addAction("Linear (amp)", lambda: self.set_db(False))
            db = m.addAction("dB", lambda: self.set_db(True))
            lin.setCheckable(True)
            db.setCheckable(True)
            (db if self.db_view else lin).setChecked(True)
            m.exec(e.globalPosition().toPoint())
        elif e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self._vzoom(2.0)
        else:
            self._vzoom(0.5)

    def _vzoom(self, f: float) -> None:
        self.vmax = float(min(max(self.vmax * f, 1 / 256), 1.0))
        self.update()

    def set_db(self, on: bool) -> None:
        self.db_view = on
        self.update()
        self.changed.emit()

    def mouseMoveEvent(self, e):
        x, y = e.position().x(), e.position().y()
        if not self._dragging:
            if y < RULER_H:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                self.setToolTip("Click to play from here")
            elif x < SCALE_W:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                self.setToolTip("Click to zoom in, Shift+click to zoom out, right-click for options")
            else:
                hit = self._hit(x, y)
                self.setCursor(Qt.CursorShape.PointingHandCursor if hit else Qt.CursorShape.IBeamCursor)
                self.setToolTip("")
            return
        if self._anchor is None:
            return
        if x < SCALE_W or x > self.width():                    # drag past an edge scrolls
            self.t0 += ((x - SCALE_W) if x < SCALE_W else x - self.width()) * self.spp * 0.5
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
        x, y = e.position().x(), e.position().y()
        if x < SCALE_W or y < RULER_H + CLIP_H:
            return
        if y >= self._label_top():
            hit = self._hit(x, y)
            if hit:
                self.item_clicked.emit("rename", hit[1])
            else:
                self.add_label_at.emit(self.t_of(x))
            return
        # select the phrase under the mouse (audio between two silences)
        tl, sr = self.ed.tl, self.ed.sr
        i = tl.seg_index(self.t_of(x) * sr)
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
        if self._fitted:
            self.spp = max(self.total, 1.0) / self.wave_w * 1.02
            self.t0 = 0.0
        self.clamp_view()
        self.changed.emit()


# ===================================================================== the editor window

class EditorDialog(QDialog):
    """Edit one take. Accept = render it. `editor.audio` is the audio the edits refer to."""

    def __init__(self, take_dir: Path, edl: EditList, audio: np.ndarray, parent=None, auto_cfg=None):
        super().__init__(parent)
        self.take_dir = Path(take_dir)
        self.take_name = _take_name(self.take_dir)
        self.setWindowTitle(f"{self.take_name} · Hush Studio editor")
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setStyleSheet(EDITOR_QSS)
        self.resize(1360, 820)
        self.auto_cfg = auto_cfg
        self.editor = Editor(edl, audio)
        self.player = Player(edl.sample_rate)
        self._job: Job | None = None
        self._speech = None                       # VAD of the current audio, reused by the detectors
        self._paused: tuple[int, int, bool] | None = None
        self._play_from = 0.0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._build_actions()
        lay.setMenuBar(self._build_menus())

        # --- toolbars: transport · tools · edit · zoom · meter
        bar = QFrame()
        bar.setObjectName("bar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(4)
        transport = QToolBar()
        transport.setIconSize(QSize(30, 30))
        for a in (self.act_pause, self.act_play, self.act_stop, self.act_start, self.act_end, self.act_loop):
            transport.addAction(a)
        row.addWidget(transport)
        tools = QToolBar()
        tools.setIconSize(QSize(22, 22))
        tools.addAction(self.act_tool_select)
        tools.addSeparator()
        for a in (self.act_cut, self.act_copy, self.act_paste, self.act_trim, self.act_silence):
            tools.addAction(a)
        tools.addSeparator()
        tools.addAction(self.act_undo)
        tools.addAction(self.act_redo)
        tools.addSeparator()
        for a in (self.act_zoom_in, self.act_zoom_out, self.act_zoom_sel, self.act_zoom_fit):
            tools.addAction(a)
        row.addWidget(tools)
        row.addSpacing(10)
        spk = QLabel()
        spk.setPixmap(make_icon("speaker", theme.MUTED, 20).pixmap(20, 20))
        row.addWidget(spk)
        self.meter = PlayMeter()
        row.addWidget(self.meter)
        row.addStretch(1)
        lay.addWidget(bar)

        # --- automatic features (Effect menu, as buttons)
        auto = QFrame()
        auto.setObjectName("bar")
        row = QHBoxLayout(auto)
        row.setContentsMargins(12, 2, 12, 2)
        row.setSpacing(4)
        self.auto_label = QLabel("")
        self.auto_label.setObjectName("muted")
        row.addWidget(self.auto_label)
        autobar = QToolBar()
        autobar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        for a in (self.act_auto, self.act_planes, self.act_norm):
            autobar.addAction(a)
        autobar.addSeparator()
        autobar.addAction(self.act_next)
        row.addWidget(autobar)
        row.addStretch(1)
        linkbar = QToolBar()
        linkbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        linkbar.addAction(self.act_aud_open)
        linkbar.addAction(self.act_aud_back)
        row.addWidget(linkbar)
        for tb in (autobar, linkbar):
            for b in tb.findChildren(QToolButton):
                b.setObjectName("text")
        lay.addWidget(auto)

        # --- tracks: control panels on the left, the waveform view on the right
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(0)
        corner = QFrame()
        corner.setObjectName("labelpanel")
        corner.setFixedHeight(RULER_H)
        left.addWidget(corner)
        left.addWidget(self._track_panel(), 1)
        left.addSpacing(GAP)
        left.addWidget(self._label_panel())
        leftw = QWidget()
        leftw.setLayout(left)
        leftw.setFixedWidth(PANEL_W)
        body.addWidget(leftw)
        self.view = WaveView(self.editor, self.take_name)
        body.addWidget(self.view, 1)
        lay.addLayout(body, 1)
        self.scroll = QScrollBar(Qt.Orientation.Horizontal)
        self.scroll.valueChanged.connect(self._scrolled)
        lay.addWidget(self.scroll)

        # --- selection toolbar
        selbar = QFrame()
        selbar.setObjectName("selbar")
        row = QHBoxLayout(selbar)
        row.setContentsMargins(14, 6, 14, 6)
        row.setSpacing(8)
        row.addWidget(QLabel("Audio Position"))
        self.f_pos = TimeField(big=True)
        self.f_pos.edited.connect(lambda t: self.move_cursor(min(t, self.editor.duration)))
        row.addWidget(self.f_pos)
        row.addSpacing(24)
        row.addWidget(QLabel("Selection"))
        self.sel_mode = QComboBox()
        self.sel_mode.setObjectName("selmode")
        self.sel_mode.addItems(["Start and End", "Start and Length"])
        self.sel_mode.currentIndexChanged.connect(lambda _: self._update_selbar(force=True))
        row.addWidget(self.sel_mode)
        self.f_start = TimeField()
        self.f_end = TimeField()
        self.f_start.edited.connect(self._sel_typed)
        self.f_end.edited.connect(self._sel_typed)
        row.addWidget(self.f_start)
        row.addWidget(self.f_end)
        row.addStretch(1)
        self.lengths = QLabel("")
        self.lengths.setObjectName("muted")
        self.lengths.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lengths.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.lengths, 1)
        lay.addWidget(selbar)

        # --- status bar
        status = QFrame()
        status.setObjectName("statusbar")
        row = QHBoxLayout(status)
        row.setContentsMargins(14, 6, 14, 8)
        row.setSpacing(10)
        self.state = QLabel("Stopped.")
        self.state.setStyleSheet("font-weight: 600;")
        row.addWidget(self.state)
        self.status = QLabel("Click to place the cursor, drag to select. Space plays.")
        self.status.setObjectName("muted")
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)   # never widens the window
        row.addWidget(self.status, 1)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setFixedWidth(120)
        self.busy.hide()
        row.addWidget(self.busy)
        self.polish = QCheckBox("Studio polish")
        self.polish.setToolTip("Warmth, softer “s” sounds, gentle evening-out (keeps her style)")
        row.addWidget(self.polish)
        close = QPushButton("Save && close")
        close.clicked.connect(self.reject)
        self.btn_render = QPushButton("Render video")
        self.btn_render.setObjectName("primary")
        self.btn_render.clicked.connect(self.accept)
        row.addWidget(close)
        row.addWidget(self.btn_render)
        lay.addWidget(status)

        # keyboard belongs to the waveform: buttons never take focus (Space / Enter / arrows)
        for wdg in self.findChildren(QToolButton) + [self.polish, close, self.btn_render, self.sel_mode]:
            wdg.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for b in (close, self.btn_render):
            b.setAutoDefault(False)

        self.view.changed.connect(self._view_changed)
        self.view.item_clicked.connect(self._item_clicked)
        self.view.context.connect(self._context_menu)
        self.view.quick_play.connect(self.quick_play)
        self.view.add_label_at.connect(lambda t: self._edit("Label added", lambda: self.editor.add_marker(t)))
        self.tick = QTimer(self, interval=40, timeout=self._tick)
        self._save_timer = QTimer(self, interval=400, singleShot=True, timeout=self.save_now)
        self.setWindowState(Qt.WindowState.WindowMaximized)
        QTimer.singleShot(0, self.view.fit)
        self.view.setFocus()
        self._after_edit(save=False)

    # ------------------------------------------------------------------ building

    def _act(self, text: str, fn, keys=(), icon: str | None = None, tip: str = "") -> QAction:
        a = QAction(make_icon(icon) if icon else QIcon(), text, self)
        if keys:
            a.setShortcuts([QKeySequence(k) for k in keys])
        a.setToolTip(f"{tip or text}" + (f"  ({keys[0]})" if keys else ""))
        a.setStatusTip(tip)
        if fn is not None:
            a.triggered.connect(lambda _=False: fn())
        self.addAction(a)                 # shortcuts work with the menus closed
        return a

    def _build_actions(self) -> None:
        v = lambda: self.view
        A = self._act
        # transport
        self.act_pause = A("Pause", self.toggle_pause, ["P"], "pause", "Pause / resume playback")
        self.act_play = A("Play", self.toggle_play, ["Space"], "play", "Play from the cursor, or the selection")
        self.act_stop = A("Stop", self.stop_play, [], "stop")
        self.act_start = A("Skip to Start", lambda: self.move_cursor(0.0), ["Home"], "skip_start")
        self.act_end = A("Skip to End", lambda: self.move_cursor(self.editor.duration), ["End"], "skip_end")
        self.act_loop = A("Loop Play", lambda: self.toggle_play(loop=True), ["Shift+Space"], "loop",
                          "Play the selection over and over")
        # tools
        self.act_tool_select = A("Selection Tool", None, ["F1"], "select", "Selection tool")
        self.act_tool_select.setCheckable(True)
        self.act_tool_select.setChecked(True)
        # edit
        self.act_undo = A("Undo", self.undo, ["Ctrl+Z"], "undo")
        self.act_redo = A("Redo", self.redo, ["Ctrl+Y", "Ctrl+Shift+Z"], "redo")
        self.act_cut = A("Cut", self.do_cut, ["Ctrl+X"], "cut")
        self.act_delete = A("Delete", self.do_delete, ["Ctrl+K", "Delete", "Backspace"], None)
        self.act_copy = A("Copy", self.do_copy, ["Ctrl+C"], "copy")
        self.act_paste = A("Paste", self.do_paste, ["Ctrl+V"], "paste", "Paste at the cursor, or over the selection")
        self.act_trim = A("Trim Audio", self.do_trim, ["Ctrl+T"], "trim", "Keep only the selection")
        self.act_silence = A("Silence Audio", self.do_silence, ["Ctrl+L"], "silence", "Make the selection digital silence")
        self.act_insert = A("Silence…", self.do_insert_silence, [], None, "Insert seconds of silence at the cursor")
        self.act_label_sel = A("Add Label at Selection", self.do_marker, ["Ctrl+B"], "label")
        self.act_label_play = A("Add Label at Playback Position", self.do_marker_playback, ["Ctrl+M"], None)
        # select
        self.act_sel_all = A("All", self.select_all, ["Ctrl+A"])
        self.act_sel_none = A("None", self.clear_selection, ["Ctrl+Shift+A", "Escape"])
        self.act_left = A("Cursor Left", lambda: self.nudge(-1), ["Left"])
        self.act_right = A("Cursor Right", lambda: self.nudge(1), ["Right"])
        self.act_ext_left = A("Selection Extend Left", lambda: self.nudge(-1, extend=True), ["Shift+Left"])
        self.act_ext_right = A("Selection Extend Right", lambda: self.nudge(1, extend=True), ["Shift+Right"])
        self.act_next = A("Next proposed cut", self.next_proposal, [], None, "Jump to the next cut waiting for you")
        # view
        self.act_zoom_in = A("Zoom In", lambda: v().zoom(2.0), ["Ctrl+1"], "zoom_in")
        self.act_zoom_normal = A("Zoom Normal", lambda: v().zoom_normal(), ["Ctrl+2"], None)
        self.act_zoom_out = A("Zoom Out", lambda: v().zoom(0.5), ["Ctrl+3"], "zoom_out")
        self.act_zoom_sel = A("Zoom to Selection", self.zoom_selection, ["Ctrl+E"], "zoom_sel")
        self.act_zoom_fit = A("Fit to Width", lambda: v().fit(), ["Ctrl+F"], "zoom_fit", "Fit the whole take")
        self.act_linear = A("Waveform", lambda: v().set_db(False), [])
        self.act_db = A("Waveform (dB)", lambda: v().set_db(True), [])
        grp = QActionGroup(self)
        for a in (self.act_linear, self.act_db):
            a.setCheckable(True)
            grp.addAction(a)
        self.act_linear.setChecked(True)
        # effects (and Hush Studio's automatic tools)
        self.act_auto = A("Auto-edit Pauses", self.do_auto_pauses, [], None,
                          "Her pause-marker word → long silence, other pauses → digital silence. Your own silences stay.")
        self.act_planes = A("Remove Plane Noise", self.do_planes, [], None,
                            "Find planes and loud noise and cut them at her pauses, like the plane guard")
        self.act_norm = A("Normalise", self.do_normalise, [], None,
                          "Selection: match the loudness of the rest of the take. Whole take: −21 LUFS")
        self.act_fade_in = A("Fade In", lambda: self.do_fade("in"), [], None)
        self.act_fade_out = A("Fade Out", lambda: self.do_fade("out"), [], None)
        # file
        self.act_save = A("Save", self.save_now, ["Ctrl+S"])
        self.act_export = A("Export Audio…", self.export_audio, ["Ctrl+Shift+E"], None, "Save the edited take as a WAV file")
        self.act_aud_open = A("Open in Audacity", self.open_in_audacity, [], None,
                              "Export the edited take and open it in Audacity, for anything heavier")
        self.act_aud_back = A("Bring back from Audacity…", self.bring_back, [], None,
                              "Use the file you exported from Audacity as the take (you can undo)")
        self.act_render = A("Render Video", self.accept, ["Ctrl+R"])
        self.act_close = A("Close", self.reject, ["Ctrl+W"])

    def _build_menus(self) -> QMenuBar:
        mb = QMenuBar(self)

        def menu(title, items, parent=mb):
            m = parent.addMenu(title)
            for it in items:
                if it is None:
                    m.addSeparator()
                elif isinstance(it, tuple):
                    menu(it[0], it[1], m)
                else:
                    m.addAction(it)
            return m

        menu("&File", [self.act_save, None, self.act_export, self.act_aud_open, self.act_aud_back, None,
                       self.act_render, self.act_close])
        menu("&Edit", [self.act_undo, self.act_redo, None, self.act_cut, self.act_delete, self.act_copy, self.act_paste,
                       None, ("Remove Special", [self.act_trim, self.act_silence]), None,
                       ("Labels", [self.act_label_sel, self.act_label_play])])
        menu("&Select", [self.act_sel_all, self.act_sel_none, None, self.act_ext_left, self.act_ext_right, None,
                         self.act_next])
        view = menu("&View", [("Zoom", [self.act_zoom_in, self.act_zoom_normal, self.act_zoom_out, self.act_zoom_sel]),
                              ("Track Size", [self.act_zoom_fit]), None, self.act_linear, self.act_db, None])
        self.goto_menu = view.addMenu("Go to Label")
        menu("&Transport", [self.act_play, self.act_pause, self.act_stop, self.act_loop, None,
                            self.act_start, self.act_end])
        menu("&Generate", [self.act_insert])
        menu("Effe&ct", [self.act_auto, self.act_planes, None, self.act_fade_in, self.act_fade_out, self.act_norm])
        return mb

    def _track_panel(self) -> QFrame:
        f = QFrame()
        f.setObjectName("trackpanel")
        v = QVBoxLayout(f)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(6)
        name = QToolButton()
        name.setText(f"{self.take_name[:16]} ▾")
        name.setToolTip("Track menu")
        name.setStyleSheet("font-weight: 600; text-align: left;")
        tm = QMenu(name)
        tm.addAction(self.act_linear)
        tm.addAction(self.act_db)
        tm.addSeparator()
        tm.addAction("Zoom Reset (vertical)", lambda: self.view.reset_vzoom())
        tm.addAction(self.act_sel_all)
        name.setMenu(tm)
        name.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        v.addWidget(name)
        info = QLabel(f"Mono, {self.editor.sr} Hz\n32-bit float")
        info.setObjectName("muted")
        info.setStyleSheet("font-size: 12px;")
        v.addWidget(info)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_mute = QToolButton()
        self.btn_mute.setText("Mute")
        self.btn_mute.setObjectName("small")
        self.btn_mute.setCheckable(True)
        self.btn_mute.toggled.connect(lambda on: setattr(self.player, "muted", on))
        sel = QToolButton()
        sel.setText("Select")
        sel.setObjectName("small")
        sel.clicked.connect(self.select_all)
        row.addWidget(self.btn_mute)
        row.addWidget(sel)
        row.addStretch(1)
        v.addLayout(row)
        v.addStretch(1)
        return f

    def _label_panel(self) -> QFrame:
        f = QFrame()
        f.setObjectName("labelpanel")
        f.setFixedHeight(LABEL_H)
        h = QHBoxLayout(f)
        h.setContentsMargins(10, 0, 10, 0)
        lbl = QLabel("Labels")
        lbl.setStyleSheet("font-weight: 600;")
        h.addWidget(lbl)
        h.addStretch(1)
        add = QToolButton()
        add.setText("+ Add")
        add.setObjectName("small")
        add.setToolTip("Add a label at the cursor (Ctrl+B)")
        add.clicked.connect(self.do_marker)
        h.addWidget(add)
        return f

    # ------------------------------------------------------------------ helpers

    @property
    def sel(self):
        return self.view.sel

    def _say(self, text: str) -> None:
        self.status.setText(text)

    def _after_edit(self, save: bool = True, label: str = "") -> None:
        self.stop_play()
        self.view.refresh()
        if save:
            self._save_timer.start()            # saved half a second after the last edit (and on close)
        if label:
            self._say(f"{label}. Undo with Ctrl+Z.")
        v = self.view
        pending = [m for m in v._marks if m.cut.proposed]
        self.act_next.setVisible(bool(pending))
        self.act_next.setText(f"Next proposed cut ({len(pending)})")
        self.goto_menu.clear()
        for pos, mk in v._markers:
            self.goto_menu.addAction(f"{mk.label}   {fmt_t(pos)}").triggered.connect(
                lambda _=False, pos=pos: self.move_cursor(pos))
        self.goto_menu.setEnabled(bool(v._markers))
        self._view_changed()

    def _view_changed(self) -> None:
        v, ed = self.view, self.editor
        has_sel = bool(v.sel and v.sel[1] - v.sel[0] > 1e-3)
        busy = self._job is not None
        for a in (self.act_cut, self.act_copy, self.act_delete, self.act_silence, self.act_trim,
                  self.act_fade_in, self.act_fade_out, self.act_zoom_sel):
            a.setEnabled(has_sel and not busy)
        self.act_paste.setEnabled(bool(ed.clipboard) and not busy)
        for a in (self.act_insert, self.act_label_sel, self.act_label_play, self.act_auto, self.act_norm,
                  self.act_aud_open, self.act_aud_back, self.act_render, self.act_export):
            a.setEnabled(not busy)
        self.btn_render.setEnabled(not busy)
        self.act_planes.setEnabled(not busy and ed.on_original)
        self.act_planes.setToolTip("Find planes and loud noise and cut them at her pauses, like the plane guard"
                                   if ed.on_original else "Not available on audio brought back from Audacity")
        self.act_undo.setEnabled(ed.can_undo and not busy)
        self.act_redo.setEnabled(ed.can_redo and not busy)
        self.act_undo.setText(f"Undo {ed.undo_label()}" if ed.can_undo else "Undo")
        self.act_redo.setText(f"Redo {ed.redo_label()}" if ed.can_redo else "Redo")
        self.act_linear.setChecked(not v.db_view)
        self.act_db.setChecked(v.db_view)
        self.auto_label.setText("Automatic, on the selection:" if has_sel else "Automatic, on the whole take:")
        self.lengths.setText(f"Edited {fmt_mmss(ed.duration)} · recorded {fmt_mmss(ed.edl.duration)}"
                             + ("" if ed.on_original else " (from Audacity)"))
        self._update_selbar()
        # scrollbar mirrors the view (milliseconds)
        span = max(0, int((v.total - v.visible_s()) * 1000))
        self.scroll.blockSignals(True)
        self.scroll.setRange(0, span)
        self.scroll.setPageStep(max(1, int(v.visible_s() * 1000)))
        self.scroll.setValue(int(v.t0 * 1000))
        self.scroll.blockSignals(False)

    def _update_selbar(self, force: bool = False) -> None:
        v = self.view
        a, b = v.sel if v.sel else (v.cursor_t, v.cursor_t)
        second = b if self.sel_mode.currentIndex() == 0 else b - a
        for f, val in ((self.f_start, a), (self.f_end, second)):
            if force or not f.hasFocus():
                f.set_seconds(val)
        if not self.f_pos.hasFocus():
            self.f_pos.set_seconds(v.playhead if v.playhead is not None else v.cursor_t)

    def _sel_typed(self, _=None) -> None:
        a = self.f_start.seconds()
        second = self.f_end.seconds()
        b = second if self.sel_mode.currentIndex() == 0 else a + second
        total = self.editor.duration
        a, b = min(a, total), min(b, total)
        if b > a:
            self.select(a, b)
        else:
            self.move_cursor(a)
        self.view.setFocus()

    def _scrolled(self, val: int) -> None:
        self.view.t0 = val / 1000
        self.view.clamp_view()
        self.view.update()

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

    def nudge(self, direction: int, extend: bool = False) -> None:
        """Arrow keys, as in Audacity: with a selection, ←/→ go to its start/end; Shift extends it."""
        v = self.view
        step = max(v.spp * 2, 0.01)
        total = self.editor.duration
        if extend:
            a, b = v.sel if v.sel else (v.cursor_t, v.cursor_t)
            if direction < 0:
                a = max(0.0, a - step)
            else:
                b = min(total, b + step)
            if b > a:
                self.select(a, b)
            return
        if v.sel:
            self.move_cursor(v.sel[0] if direction < 0 else v.sel[1])
        else:
            self.move_cursor(min(max(v.cursor_t + direction * step, 0.0), total))

    def zoom_selection(self) -> None:
        if self.view.sel:
            self.view.show_range(*self.view.sel)

    # ------------------------------------------------------------------ playback

    def toggle_play(self, loop: bool = False) -> None:
        if self.player.playing or self._paused:
            self.stop_play()
            return
        ed = self.editor
        if self.view.sel and self.view.sel[1] - self.view.sel[0] > 0.02:
            a, b = self.view.sel
        else:
            a, b = self.view.cursor_t, ed.duration
            if a >= ed.duration - 0.05:
                a = 0.0
        self._start(int(a * ed.sr), int(b * ed.sr), loop)

    def quick_play(self, t: float) -> None:
        """Click on the timeline: play from there (Audacity's Quick-Play)."""
        self.stop_play()
        self.view.cursor_t = t
        self.view.sel = None
        self._start(int(t * self.editor.sr), int(self.editor.duration * self.editor.sr), False)

    def _start(self, a: int, b: int, loop: bool) -> None:
        tl, audio = self.editor.tl, self.editor.audio
        self._paused = None
        if self.player.play(lambda i, j: tl.render(audio, i, j), a, b, loop=loop):
            self.state.setText("Looping." if loop else "Playing.")
            self.tick.start()
        else:
            self._say(self.player.error)

    def toggle_pause(self) -> None:
        if self._paused:
            pos, end, loop = self._paused
            self._start(pos, end, loop)
        elif self.player.playing:
            self._paused = (self.player.position(), self.player.end, self.player.loop)
            self.player.stop()
            self.tick.stop()
            self.state.setText("Paused.")

    def stop_play(self) -> None:
        self.player.stop()
        self._paused = None
        self.tick.stop()
        self.view.playhead = None
        self.meter.set_db(-120.0)
        self.state.setText("Stopped.")
        self.view.update()
        self._update_selbar()

    def _tick(self) -> None:
        if not self.player.playing:
            self.stop_play()
            return
        t = self.player.position() / self.editor.sr
        self.view.playhead = t
        self.meter.set_db(self.player.peak_db)
        if t > self.view.t0 + self.view.visible_s() or t < self.view.t0:
            self.view.t0 = t - self.view.visible_s() * 0.05
            self.view.clamp_view()
            self._view_changed()
        if not self.f_pos.hasFocus():
            self.f_pos.set_seconds(t)
        self.view.update()

    def listen_source(self, a: float, b: float) -> None:
        """Play source audio (e.g. what a cut removed), with a second either side."""
        sr = self.editor.sr
        x = self.editor.audio[max(0, int((a - 1) * sr)): int((b + 1) * sr)]
        self.stop_play()
        if self.player.play_array(np.ascontiguousarray(x)):
            self.state.setText("Playing (what was cut).")
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
            self._say(f"Copied {self.sel[1] - self.sel[0]:.1f} s. Click where it should go and paste (Ctrl+V).")
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

    def do_trim(self):
        if self.sel:
            a, b = self.sel
            self._edit("Trim audio", lambda: self.editor.trim(a, b))
            self.select(0.0, self.editor.duration)

    def do_silence(self):
        if self.sel:
            self._edit("Silence", lambda: self.editor.silence(*self.sel))

    def do_insert_silence(self):
        secs, ok = QInputDialog.getDouble(self, "Silence", "Seconds of silence to insert at the cursor:", 5.0, 0.1, 600.0, 1)
        if ok:
            at = self.sel[0] if self.sel else self.view.cursor_t
            self._edit("Insert silence", lambda: self.editor.insert_silence(at, secs))

    def do_fade(self, kind: str):
        if self.sel:
            self._edit("Fade in" if kind == "in" else "Fade out", lambda: self.editor.fade(*self.sel, kind))

    def do_marker(self):
        at = self.sel[0] if self.sel else self.view.cursor_t
        self._edit("Label added", lambda: self.editor.add_marker(at, f"Label {len(self.editor.edl.markers) + 1}"))

    def do_marker_playback(self):
        at = self.view.playhead if self.view.playhead is not None else self.view.cursor_t
        self._edit("Label added", lambda: self.editor.add_marker(at, f"Label {len(self.editor.edl.markers) + 1}"))

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

    # ------------------------------------------------------------------ cuts, proposals, labels

    def next_proposal(self):
        pending = sorted((m for m in self.view._marks if m.cut.proposed), key=lambda m: m.a)
        if not pending:
            return
        after = [m for m in pending if m.a > self.view.cursor_t + 1e-3] or pending
        m = after[0]
        self.view.show_range(m.a - 3, m.b + 3)
        self.select(m.a, m.b)
        self._say("Proposed cut: Space to listen, then click its amber bar to accept or undo it.")

    def _item_clicked(self, kind: str, obj) -> None:
        if kind == "rename":
            self._rename_marker(obj[1])
            return
        menu = QMenu(self)
        if kind == "marker":
            pos, mk = obj
            menu.addAction(f"Go to {mk.label}", lambda: self.move_cursor(pos))
            menu.addAction("Rename…", lambda: self._rename_marker(mk))
            menu.addAction("Delete label", lambda: self._edit("Label deleted", lambda: self.editor.remove_marker(mk.id)))
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
        text, ok = QInputDialog.getText(self, "Rename label", "Label:", text=mk.label)
        if ok and text.strip():
            self._edit("Label renamed", lambda: self.editor.rename_marker(mk.id, text.strip()))

    def _context_menu(self, pos) -> None:
        menu = QMenu(self)
        for it in (self.act_play, None, self.act_cut, self.act_copy, self.act_paste, self.act_delete, None,
                   self.act_trim, self.act_silence, self.act_insert, None, self.act_fade_in, self.act_fade_out,
                   self.act_norm, None, self.act_label_sel):
            if it is None:
                menu.addSeparator()
            else:
                menu.addAction(it)
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
        self.state.setText("Working…")
        self._say(text)
        self._view_changed()
        self._job.start()

    def _job_done(self, on_done, result) -> None:
        self._job = None
        self.busy.hide()
        self.state.setText("Stopped.")
        try:
            on_done(result)
        except Exception as e:
            self._say(f"That didn't work: {e}")
        self._view_changed()

    def _job_failed(self, msg: str) -> None:
        self._job = None
        self.busy.hide()
        self.state.setText("Stopped.")
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
        audio, edl_copy, cfg = ed.audio, EditList.from_dict(ed.edl.to_dict()), self.auto_cfg

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
        audio, edl_copy = ed.audio, EditList.from_dict(ed.edl.to_dict())

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
            self._say(f"{n} plane / noise cut{'s' if n != 1 else ''} in {where} (▼ on the clip bar marks each "
                      f"one: click to listen or restore)." if n else f"No planes or loud noise found in {where}.")
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

    # ------------------------------------------------------------------ files: export, Audacity round trip

    def _write_edited(self, path: Path):
        ed = self.editor
        tl, audio, sr = ed.tl, ed.audio, ed.sr

        def work():
            import soundfile as sf
            sf.write(path, tl.render(audio), sr, subtype="FLOAT")
            return path
        return work

    def export_audio(self):
        f, _ = QFileDialog.getSaveFileName(self, "Export Audio", str(self.take_dir / f"{self.take_name}.wav"),
                                           "WAV (*.wav)")
        if f:
            self._run("Exporting the edited take…", self._write_edited(Path(f)),
                      lambda p: self._say(f"Exported to {p}."))

    def open_in_audacity(self):
        path = self.take_dir / "for_audacity.wav"

        def done(p):
            exe = find_audacity()
            if exe:
                try:
                    subprocess.Popen([exe, str(p)])
                    self._say("Opened in Audacity. When you're done there: File → Export Audio (WAV), "
                              "then File → Bring back from Audacity.")
                    return
                except OSError:
                    pass
            QMessageBox.information(self, "Audacity", "Audacity isn't installed here (audacityteam.org).\n\n"
                                    f"The edited take is saved as {p.name} in the take's folder; "
                                    "any audio editor can open it. Then use Bring back from Audacity.")
            if sys.platform == "win32":
                subprocess.run(["explorer", "/select,", str(p)])
        self._run("Saving the edited take for Audacity…", self._write_edited(path), done)

    def bring_back(self):
        f, _ = QFileDialog.getOpenFileName(self, "The file you exported from Audacity", str(self.take_dir),
                                           "Audio (*.wav *.flac *.mp3 *.m4a *.aiff *.ogg)")
        if not f:
            return
        if QMessageBox.question(self, "Bring back from Audacity",
                                "Use this file as the take from now on? Your edits so far are in it already, "
                                "so the edit list starts fresh (labels are kept). You can undo this.") \
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
            self.view.reset_vzoom()
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


def _take_name(d: Path) -> str:
    try:
        return json.loads((d / "take.json").read_text("utf-8")).get("name") or d.name
    except (OSError, ValueError):
        return d.name
