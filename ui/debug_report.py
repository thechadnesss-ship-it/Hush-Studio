"""Help > Send debug report: build one zip (logs, system, settings, take measurements; audio only
if she ticks a clip), save it to a flash drive / folder and open that folder (T20)."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton,
                             QScrollArea, QVBoxLayout, QWidget)

from core import debug_bundle as db
from core.log_setup import get_logger
from core.renderer import fmt_mmss

from . import theme
from .review import play


def open_folder(path: Path) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["explorer", "/select,", str(path)] if path.is_file() else ["explorer", str(path)])
        else:
            subprocess.run(["xdg-open", str(path if path.is_dir() else path.parent)])
    except Exception:
        pass


class DebugReportDialog(QDialog):
    """`sessions`: take folders, newest first; `current`: the take on screen (preselected)."""

    def __init__(self, sessions: list[Path], current: Path | None, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Send debug report")
        self.setMinimumWidth(520)
        self.settings_dict = settings
        self.sessions = list(sessions)
        self.result_path: Path | None = None
        self.spots: list[dict] = []
        self.ticks: dict[str, QCheckBox] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.setSpacing(10)
        title = QLabel("Send debug report")
        title.setStyleSheet("font-size:18px; font-weight:500;")
        lay.addWidget(title)
        note = QLabel(db.WHAT_IS_INCLUDED)
        note.setWordWrap(True)
        note.setObjectName("muted")
        lay.addWidget(note)

        row = QHBoxLayout()
        row.addWidget(QLabel("Recording"))
        self.take = QComboBox()
        self.take.addItem("(none, just logs)", None)
        for s in self.sessions:
            self.take.addItem(s.name, s)
        i = self.take.findData(current) if current in self.sessions else (1 if self.sessions else 0)
        self.take.setCurrentIndex(max(i, 0))
        self.take.currentIndexChanged.connect(self._fill_clips)
        row.addWidget(self.take, 1)
        lay.addLayout(row)

        self.clips_head = QLabel("")
        self.clips_head.setWordWrap(True)
        lay.addWidget(self.clips_head)
        self.clips_box = QVBoxLayout()
        holder = QWidget()
        holder.setLayout(self.clips_box)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setMaximumHeight(170)
        self.scroll.setWidget(holder)
        lay.addWidget(self.scroll)

        row = QHBoxLayout()
        row.addWidget(QLabel("Save to"))
        self.where = QComboBox()
        for label, p in db.save_locations():
            self.where.addItem(label, p)
        self.where.addItem("Choose another folder…", None)
        self.where.activated.connect(self._where_chosen)
        row.addWidget(self.where, 1)
        lay.addLayout(row)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        btns = QHBoxLayout()
        self.btn_flash = QPushButton("Copy to flash drive")
        self.btn_flash.clicked.connect(self.copy_to_flash)
        self.btn_flash.hide()
        btns.addWidget(self.btn_flash)
        btns.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        self.btn_make = QPushButton("Save debug report")
        self.btn_make.setObjectName("primary")
        self.btn_make.clicked.connect(self.make)
        btns.addWidget(close)
        btns.addWidget(self.btn_make)
        lay.addLayout(btns)
        self._fill_clips()

    # ------------------------------------------------------------------ clips

    def _fill_clips(self):
        while self.clips_box.count():
            w = self.clips_box.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.ticks.clear()
        d = self.take.currentData()
        self.spots = []
        if d:
            try:
                self.spots = db.flagged_spots(d)
            except Exception:
                self.spots = []
        if not d:
            self.clips_head.setText("")
        elif not self.spots:
            self.clips_head.setText("No clipping or dropouts were found in this take, so there are no clips to add.")
        else:
            self.clips_head.setText("Short clips around the problem spots. Tick any you are happy to share "
                                    "(none are ticked, so no sound is sent unless you choose it):")
        for s in self.spots:
            r = QHBoxLayout()
            box = QCheckBox(f"{fmt_mmss(s['t_s'])}  {s['kind']}")
            box.setChecked(False)
            self.ticks[s["id"]] = box
            btn = QPushButton("▶  Play")
            btn.setObjectName("link")
            btn.clicked.connect(lambda _=False, s=s, d=d: self._play(d, s))
            r.addWidget(box, 1)
            r.addWidget(btn)
            w = QWidget()
            w.setLayout(r)
            self.clips_box.addWidget(w)
        self.clips_box.addStretch(1)
        self.scroll.setVisible(bool(self.spots))

    def _play(self, d, spot):
        try:
            x, _ = db.read_clip(d, spot)
            play(x)
        except Exception:
            self.status.setText("Could not play that clip.")

    def ticked(self) -> list[str]:
        return [i for i, b in self.ticks.items() if b.isChecked()]

    # ------------------------------------------------------------------ saving

    def _where_chosen(self, idx: int):
        if self.where.itemData(idx) is None:
            f = QFileDialog.getExistingDirectory(self, "Save debug report in…")
            if f:
                self.where.insertItem(idx, f, Path(f))
                self.where.setCurrentIndex(idx)
            else:
                self.where.setCurrentIndex(0)

    def make(self):
        dest = self.where.currentData()
        if dest is None:
            return
        try:
            self.result_path = db.build_bundle(self.take.currentData(), dest, self.settings_dict,
                                               clip_ids=self.ticked())
        except Exception as e:
            get_logger("app").exception("debug bundle failed")
            self.status.setText(f"Could not save the report: {e}")
            self.status.setStyleSheet(f"color:{theme.RED};")
            return
        get_logger("app").info("debug bundle saved (%d clips)", len(self.ticked()))
        n = len(self.ticked())
        self.status.setText(f"Saved: {self.result_path.name}\n"
                            + (f"It includes {n} audio clip{'s' if n != 1 else ''}." if n else "It contains no audio."))
        self.status.setStyleSheet(f"color:{theme.GREEN};")
        self.btn_flash.setVisible(bool(db.removable_drives()) and
                                  not any(self.result_path.anchor == str(d) for d in db.removable_drives()))
        open_folder(self.result_path)

    def copy_to_flash(self):
        drives = db.removable_drives()
        if not (self.result_path and drives):
            self.status.setText("No flash drive found. Plug it in and try again.")
            return
        try:
            out = shutil.copy2(self.result_path, drives[0] / self.result_path.name)
        except OSError as e:
            self.status.setText(f"Could not copy to the flash drive: {e}")
            return
        self.status.setText(f"Copied to {drives[0]}. You can safely remove the drive once Windows says so.")
        open_folder(Path(out))
