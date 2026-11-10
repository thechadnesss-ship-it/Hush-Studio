"""Sky forecast dialog: weekly heatmap of expected planes + today's quietest windows."""
from __future__ import annotations

from datetime import datetime

import numpy as np
from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from core.quiet_planner.forecast import Forecast, best_windows

from . import theme

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _mix(a: str, b: str, t: float) -> QColor:
    ca, cb = QColor(a), QColor(b)
    return QColor(int(ca.red() + (cb.red() - ca.red()) * t), int(ca.green() + (cb.green() - ca.green()) * t),
                  int(ca.blue() + (cb.blue() - ca.blue()) * t))


class Heatmap(QWidget):
    def __init__(self, fc: Forecast, now: datetime, parent=None):
        super().__init__(parent)
        self.fc, self.now = fc, now
        self.setMinimumSize(640, 230)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        left, top = 44, 22
        cw = (self.width() - left - 6) / 24
        ch = (self.height() - top - 6) / 7
        vmax = max(1.0, float(np.nanmax(self.fc.rate)))
        p.setFont(QFont(theme.FONT_FAMILY, 8))
        for h in range(0, 24, 3):
            p.setPen(QColor(theme.MUTED))
            p.drawText(QRectF(left + h * cw, 0, cw * 3, top - 4),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, f"{h:02d}")
        for d in range(7):
            p.setPen(QColor(theme.MUTED))
            p.drawText(QRectF(0, top + d * ch, left - 8, ch),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, DAYS[d])
            for h in range(24):
                v = float(self.fc.rate[d, h]) / vmax
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_mix(theme.LILAC_SOFT, theme.ACCENT, min(1.0, v)))
                p.drawRoundedRect(QRectF(left + h * cw + 1, top + d * ch + 1, cw - 2, ch - 2), 3, 3)
        d = self.now.weekday()                              # outline today's row
        p.setPen(QPen(QColor(theme.TEXT), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(left, top + d * ch, 24 * cw, ch), 4, 4)


class SkyDialog(QDialog):
    def __init__(self, fc: Forecast, now: datetime | None = None, parent=None):
        super().__init__(parent)
        now = now or datetime.now()
        self.setWindowTitle("Sky forecast")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        title = QLabel("When is the sky quiet?")
        title.setStyleSheet("font-size:18px; font-weight:500;")
        lay.addWidget(title)
        sub = QLabel("Expected planes overhead per hour. Lighter = quieter."
                     + (f"  ({fc.learning_label()})" if fc.learning else
                        f"  (from {fc.observed_days:.0f} days of your sky, {fc.passes_logged} planes)"))
        sub.setObjectName("muted")
        lay.addWidget(sub)
        lay.addWidget(Heatmap(fc, now), 1)
        row = QHBoxLayout()
        lbl = QLabel("Quietest today:")
        lbl.setStyleSheet("font-weight:500;")
        row.addWidget(lbl)
        self.windows = best_windows(fc, now)
        for w in self.windows:
            chip = QLabel(w.label())
            chip.setStyleSheet(f"background:{theme.LILAC_SOFT}; color:{theme.ACCENT}; border-radius:14px; padding:6px 12px;")
            row.addWidget(chip)
        if not self.windows:
            row.addWidget(QLabel("no full quiet hour left today"))
        row.addStretch(1)
        lay.addLayout(row)
        note = QLabel("A forecast, not a promise: schedules change, and the plane guard still runs while you record.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        lay.addWidget(note)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        lay.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        self.resize(720, 420)
