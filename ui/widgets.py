"""Custom widgets: plane-light status pill, Riverside-style record button, level meter, companion.

The companion shows your own sprite GIFs from assets/ (companion_idle/rec/render/done.gif) when
present (personal use), else a small drawn sleepy moon.
"""
from __future__ import annotations

import time

from pathlib import Path

from PyQt6.QtCore import QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QAbstractButton, QHBoxLayout, QLabel, QSizePolicy, QWidget

from . import theme

LIGHT_TEXT = {
    "idle": "Ready",
    "green": "Clear to speak",
    "amber": "Plane coming, finish your sentence",
    "red": "Holding, plane overhead",
    "clear": "All clear, carry on",
    "paused": "Paused",
}


class TrafficLight(QWidget):
    """The plane light as a calm pill: coloured dot + words. CLEAR pulses gently."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = "idle"
        self.text = LIGHT_TEXT["idle"]
        self._pulse = False
        self._timer = QTimer(self, interval=450, timeout=self._blink)
        self.setFixedHeight(36)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._resize()

    def set_state(self, state: str, text: str | None = None) -> None:
        text = text or LIGHT_TEXT.get(state, state)
        if state == self.state and text == self.text:
            return
        self.state, self.text = state, text
        (self._timer.start() if state == "clear" else self._timer.stop())
        self._pulse = False
        self._resize()
        self.update()

    def _resize(self):
        f = QFont(theme.FONT_FAMILY, 11)
        f.setWeight(QFont.Weight.Medium)
        from PyQt6.QtGui import QFontMetrics
        self.setFixedWidth(QFontMetrics(f).horizontalAdvance(self.text) + 58)

    def _blink(self):
        self._pulse = not self._pulse
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(theme.LIGHT_COLORS.get(self.state, theme.MUTED))
        bg = QColor(c)
        bg.setAlpha(26 if not self._pulse else 52)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        p.setPen(QPen(QColor(c.red(), c.green(), c.blue(), 70), 1))
        p.setBrush(bg)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawEllipse(QRectF(16, self.height() / 2 - 5, 10, 10))
        f = QFont(theme.FONT_FAMILY, 11)
        f.setWeight(QFont.Weight.Medium)
        p.setFont(f)
        p.setPen(QColor(theme.TEXT))
        p.drawText(QRectF(36, 0, self.width() - 44, self.height()), Qt.AlignmentFlag.AlignVCenter, self.text)


class RecordButton(QAbstractButton):
    """Big round record button (Riverside-style): red dot to start, red square while recording."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.recording = False
        self.setFixedSize(QSize(84, 84))
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_recording(self, on: bool):
        self.recording = on
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(3, 3, self.width() - 6, self.height() - 6)
        ring = QColor(theme.REC_RED if (self.underMouse() or self.recording) and self.isEnabled() else theme.BORDER)
        p.setPen(QPen(ring, 3))
        p.setBrush(QColor(theme.BG))
        p.drawEllipse(r)
        fill = QColor(theme.REC_RED if self.isEnabled() else theme.LILAC)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        c = r.center()
        if self.recording:
            s = 24
            p.drawRoundedRect(QRectF(c.x() - s / 2, c.y() - s / 2, s, s), 5, 5)
        else:
            d = 34
            p.drawEllipse(QRectF(c.x() - d / 2, c.y() - d / 2, d, d))

    def enterEvent(self, e):
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.update()
        super().leaveEvent(e)


class VUMeter(QWidget):
    """Slim level meter, −60…0 dBFS, purple fill on a lilac track, peak tick.

    Clipping (T7): the whole meter turns red and stays red for `CLIP_HOLD_S` after the last
    clipped block, so a single clip is visible; the peak tick turns amber within 6 dB of it."""

    CLIP_HOLD_S = 2.0
    HOT_DB = -6.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.level = -120.0
        self.peak = -120.0
        self._clip_until = 0.0
        self.setFixedHeight(6)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    @property
    def clipping(self) -> bool:
        return time.monotonic() < self._clip_until

    def set_levels(self, rms_db: float, peak_db: float, clipped: bool = False) -> None:
        self.level = rms_db
        self.peak = max(peak_db, self.peak - 1.5)
        if clipped:
            self._clip_until = time.monotonic() + self.CLIP_HOLD_S
        self.update()

    def reset(self) -> None:
        self.level = self.peak = -120.0
        self._clip_until = 0.0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.LILAC_SOFT))
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        clip = self.clipping
        frac = lambda db: min(1.0, max(0.0, (db + 60) / 60))
        if clip:                                   # the whole track: impossible to miss
            p.setBrush(QColor(theme.REC_RED))
            p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        elif frac(self.level) > 0:
            col = theme.ACCENT if self.level < -3 else theme.REC_RED
            p.setBrush(QColor(col))
            p.drawRoundedRect(QRectF(0, 0, max(h, w * frac(self.level)), h), h / 2, h / 2)
        if self.peak > -60:
            col = theme.TEXT if clip else (theme.AMBER if self.peak >= self.HOT_DB else theme.TEXT)
            p.setBrush(QColor(col))
            x = w * frac(self.peak)
            p.drawRect(QRectF(x - 1, 0, 2, h))


COMPANION_LINES = {
    "idle": "Ready when you are.",
    "recording": "Listening. Noise filter on, I'm watching the sky.",
    "amber": "Plane in ~{s}s, finish your sentence.",
    "red": "Holding. I'll tell you when it's clear.",
    "clear": "All clear! Carry on whenever you're ready.",
    "paused": "Paused. Take a breath.",
    "preparing": "Tidying the take: noise, planes and pauses…",
    "review": "Have a look at the edits, then render.",
    "mastering": "Polishing your session…",
    "rendering": "Making your {h} hour video…",
    "success": "Done! Your video is ready.",
    "error": "Something went wrong. Details below.",
}

SPRITE_FOR_MOOD = {
    "idle": "idle", "paused": "idle", "red": "idle", "error": "idle",
    "recording": "rec", "amber": "rec", "clear": "rec",
    "preparing": "render", "review": "render", "mastering": "render", "rendering": "render",
    "success": "done",
}


class Companion(QWidget):
    """Small sprite (your GIFs if present, else a drawn moon) + one quiet line of text."""

    def __init__(self, assets_dir=None, parent=None):
        super().__init__(parent)
        self.mood = "idle"
        self.assets_dir = Path(assets_dir) if assets_dir else None
        self._movies = {}
        self.face = _MoonFace(self)
        self.sprite = QLabel(self)
        self.sprite.setFixedSize(44, 44)
        self.sprite.setScaledContents(True)
        self.sprite.hide()
        self.bubble = QLabel(COMPANION_LINES["idle"], self)
        self.bubble.setObjectName("bubble")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addStretch(1)
        lay.addWidget(self.face)
        lay.addWidget(self.sprite)
        lay.addWidget(self.bubble)
        lay.addStretch(1)
        self._show_sprite("idle")

    def _movie(self, name: str):
        from PyQt6.QtGui import QMovie
        if self.assets_dir is None:
            return None
        if name not in self._movies:
            f = self.assets_dir / f"companion_{name}.gif"
            self._movies[name] = QMovie(str(f)) if f.exists() else None
        return self._movies[name]

    def _show_sprite(self, mood: str) -> None:
        mv = self._movie(SPRITE_FOR_MOOD.get(mood, "idle"))
        if mv is not None and mv.isValid():
            if self.sprite.movie() is not mv:
                self.sprite.setMovie(mv)
                mv.start()
            self.sprite.show()
            self.face.hide()
        else:
            self.sprite.hide()
            self.face.show()

    def say(self, mood: str, text: str | None = None, **fmt) -> None:
        self.mood = mood
        self.face.mood = mood
        self.face.update()
        self._show_sprite(mood)
        self.bubble.setText(text or COMPANION_LINES.get(mood, "").format(**fmt))


class _MoonFace(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.mood = "idle"
        self.setFixedSize(34, 34)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(2, 2, 30, 30)
        moon = QPainterPath()
        moon.addEllipse(r)
        bite = QPainterPath()
        bite.addEllipse(r.translated(10, -5))
        p.fillPath(moon.subtracted(bite), QBrush(QColor(theme.LILAC)))
        p.setPen(QPen(QColor(theme.ACCENT), 1.6))
        if self.mood in ("red", "amber", "error"):
            p.setBrush(QColor(theme.ACCENT))
            p.drawEllipse(QRectF(8, 14, 3, 3.5))
            p.drawEllipse(QRectF(14, 15, 3, 3.5))
        else:
            p.drawArc(QRectF(7, 13, 5, 4), 200 * 16, 140 * 16)
            p.drawArc(QRectF(13, 14.5, 5, 4), 200 * 16, 140 * 16)
        if self.mood in ("success", "clear"):
            p.drawArc(QRectF(9, 19, 7, 4), 200 * 16, 140 * 16)
