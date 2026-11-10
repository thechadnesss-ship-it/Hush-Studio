"""Review dialog: every automatic edit, before anything is rendered.

* Plane / noise cuts: ▶ preview and "Keep anyway".
* Pause markers (Sam's spoken marker word → silence): ▶ preview, on/off, and one length for all.
* Live summary: cuts removed, markers, output length → what the long video will be.
* Recording check (live takes): sound lost while recording (computer too busy) and mic
  clipping, from capture.json, with ▶ for each spot, so a bad take is obvious before render.
"""
from __future__ import annotations

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QDialog, QDoubleSpinBox, QHBoxLayout, QHeaderView, QLabel,
                             QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from core.edl import EditList
from core.renderer import fmt_hms, fmt_mmss

from . import theme

MAX_LOST_BUTTONS = 8

SR = 48000


def play(x: np.ndarray) -> None:
    try:
        import sounddevice as sd
        sd.stop()
        sd.play(x, SR)
    except Exception:
        pass


class ReviewDialog(QDialog):
    def __init__(self, edl: EditList, clean: np.ndarray, parent=None, capture: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Review edits")
        self.resize(760, 620)
        self.edl, self.clean = edl, clean
        self._removed_markers = []
        lay = QVBoxLayout(self)

        self.summary = QLabel()
        self.summary.setStyleSheet("font-size:16px; font-weight:500;")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        self.check = QLabel()
        self.check.setWordWrap(True)
        self.check.setVisible(capture is not None)          # imported takes: nothing to report
        lay.addWidget(self.check)
        self.lost_row = QHBoxLayout()
        lay.addLayout(self.lost_row)
        if capture is not None:
            self._show_capture(capture)

        # --- cuts
        lay.addWidget(QLabel("Cuts (planes and noise). Untick to keep that part."))
        self.cuts = QTableWidget(0, 5)
        self.cuts.setHorizontalHeaderLabels(["At", "Length", "Why", "Cut", ""])
        self.cuts.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.cuts.verticalHeader().setVisible(False)
        lay.addWidget(self.cuts, 2)

        # --- markers
        row = QHBoxLayout()
        row.addWidget(QLabel("Pause markers → silence of"))
        self.marker_len = QDoubleSpinBox(suffix=" s", minimum=1.0, maximum=60.0, singleStep=0.5)
        markers = [p for p in edl.pauses if p.kind == "marker"]
        self.marker_len.setValue(markers[0].new_length if markers else 10.0)
        self.marker_len.valueChanged.connect(self._set_marker_len)
        row.addWidget(self.marker_len)
        row.addStretch(1)
        lay.addLayout(row)
        self.markers = QTableWidget(0, 4)
        self.markers.setHorizontalHeaderLabels(["At", "Original gap", "Use", ""])
        self.markers.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.markers.verticalHeader().setVisible(False)
        lay.addWidget(self.markers, 3)

        self.polish = QCheckBox("Studio polish: warmth, softer “s” sounds, gentle evening-out (keeps her style)")
        lay.addWidget(self.polish)
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Back")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Render video")
        ok.setObjectName("primary")
        ok.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay.addLayout(btns)
        self._fill()

    # ------------------------------------------------------------------ recording check

    def _show_capture(self, cap: dict) -> None:
        spots = list(cap.get("lost_at_s", []))
        raw_lost = int(cap.get("input_overflows", 0)) + int(cap.get("dropped_blocks", 0))
        # T8 reports coalesced loss spots (one per 0.5 s), so this is the count
        # the listener can actually review. Fall back for older capture.json files.
        lost = len(spots) if spots else raw_lost
        clipped = int(cap.get("clipped_samples", 0))
        self.lost_count, self.clipped_count = lost, clipped
        if not lost and not clipped:
            self.check.setText("Recording check: no lost sound, no clipping.")
            self.check.setStyleSheet(f"color:{theme.GREEN};")
            return
        lines = []
        if lost:
            lines.append(f"Sound was lost {lost} time{'s' if lost != 1 else ''} while recording "
                         f"(the computer was too busy). Listen to {'each spot' if spots else 'the take'}; "
                         "if it sounds cut, record again.")
        if clipped:
            lines.append(f"The mic clipped ({clipped:,} samples): parts may sound harsh. "
                         "Turn the mic level down before the next take.")
        self.check.setText("\n".join(lines))
        self.check.setStyleSheet(f"color:{theme.RED}; font-weight:600;")
        for t in spots[:MAX_LOST_BUTTONS]:
            btn = self._play_btn(t, t)
            btn.setText(f"▶  {fmt_mmss(t)}")
            self.lost_row.addWidget(btn)
        if len(spots) > MAX_LOST_BUTTONS:
            more = QLabel(f"+{len(spots) - MAX_LOST_BUTTONS} more")
            more.setObjectName("muted")
            self.lost_row.addWidget(more)
        self.lost_row.addStretch(1)

    # ------------------------------------------------------------------ tables

    def _fill(self):
        self.cuts.setRowCount(0)
        for c in self.edl.cuts:
            r = self.cuts.rowCount()
            self.cuts.insertRow(r)
            self.cuts.setItem(r, 0, QTableWidgetItem(fmt_mmss(c.start)))
            self.cuts.setItem(r, 1, QTableWidgetItem(f"{c.length:.1f} s"))
            self.cuts.setItem(r, 2, QTableWidgetItem(c.reason))
            box = QCheckBox()
            box.setChecked(not c.keep)
            box.toggled.connect(lambda on, c=c: (setattr(c, "keep", not on), self._update()))
            self.cuts.setCellWidget(r, 3, _centered(box))
            self.cuts.setCellWidget(r, 4, self._play_btn(c.start, c.end))
        self.markers.setRowCount(0)
        for p in [p for p in self.edl.pauses if p.kind == "marker"]:
            r = self.markers.rowCount()
            self.markers.insertRow(r)
            self.markers.setItem(r, 0, QTableWidgetItem(fmt_mmss(p.start)))
            self.markers.setItem(r, 1, QTableWidgetItem(f"{p.length:.1f} s"))
            box = QCheckBox()
            box.setChecked(True)
            box.toggled.connect(lambda on, p=p: self._toggle_marker(p, on))
            self.markers.setCellWidget(r, 2, _centered(box))
            self.markers.setCellWidget(r, 3, self._play_btn(p.start, p.end))
        self._update()

    def _play_btn(self, a: float, b: float) -> QPushButton:
        btn = QPushButton("▶  Listen")
        btn.setObjectName("link")
        lo, hi = max(0, int((a - 2) * SR)), min(len(self.clean), int((b + 2) * SR))
        btn.clicked.connect(lambda: play(self.clean[lo:hi]))
        return btn

    def _toggle_marker(self, p, on: bool):
        if on and p in self._removed_markers:
            self._removed_markers.remove(p)
            self.edl.pauses.append(p)
        elif not on and p in self.edl.pauses:
            self.edl.pauses.remove(p)          # not a marker: keep that stretch of audio as recorded
            self._removed_markers.append(p)
        self._update()

    def _set_marker_len(self, v: float):
        for p in self.edl.pauses + self._removed_markers:
            if p.kind == "marker" and not p.locked:
                p.new_length = v
        self._update()

    def _update(self):
        s = self.edl.summary()
        cuts = s["cuts"]
        planes = cuts.get("plane", {"count": 0, "removed_s": 0})
        n_mark = sum(p.kind == "marker" for p in self.edl.pauses)
        self.summary.setText(
            f"{planes['count']} plane cut{'s' if planes['count'] != 1 else ''} "
            f"({fmt_hms(planes['removed_s']).replace('0h ', '')} removed) · "
            f"{n_mark} pause markers → {self.marker_len.value():g} s silence · "
            f"session {fmt_mmss(self.edl.output_duration())}")


def _centered(w: QWidget) -> QWidget:
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(w)
    return box
