"""Intros screen (T15): upload a video/picture intro, preview, name, choose the active one, keep older ones."""
from __future__ import annotations

import os

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QAbstractItemView, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                             QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from core.intros import FILE_FILTER, Intro, IntroError, IntroLibrary


class IntrosDialog(QDialog):
    def __init__(self, library: IntroLibrary, parent=None, start_dir: str = ""):
        super().__init__(parent)
        self.lib = library
        self.start_dir = start_dir
        self.setWindowTitle("Intros")
        self.resize(720, 440)
        lay = QVBoxLayout(self)
        note = QLabel("The intro card plays silently at the start of every video, before the black screen. "
                      "Pick which one is used; older ones are kept.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        lay.addWidget(note)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["", "Name", "Length", "Size"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for i in (0, 2, 3):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._sync)
        lay.addWidget(self.table, 1)
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        lay.addWidget(self.msg)
        row = QHBoxLayout()
        self.btns: dict[str, QPushButton] = {}
        for key, text, fn in [("add", "Upload new intro…", self.upload), ("use", "Use this one", self.use),
                              ("none", "No intro", self.no_intro), ("preview", "Preview", self.preview),
                              ("rename", "Rename", self.rename), ("delete", "Delete", self.delete)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            self.btns[key] = b
            row.addWidget(b)
        self.btns["add"].setObjectName("primary")
        lay.addLayout(row)
        self.refresh()

    def current(self) -> Intro | None:
        r = self.table.currentRow()
        items = self.lib.list()
        return items[r] if 0 <= r < len(items) else None

    def refresh(self, select: str = ""):
        items = self.lib.list()
        self.table.setRowCount(len(items))
        for r, i in enumerate(items):
            mark = "● In use" if i.id == self.lib.active_id else ""
            for c, t in enumerate([mark, i.name, f"{i.duration_s:.0f} s", f"{i.width}×{i.height}"]):
                self.table.setItem(r, c, QTableWidgetItem(t))
            if i.id == (select or self.lib.active_id):
                self.table.selectRow(r)
        if not items:
            self.msg.setText("No intros yet. Upload your intro card (a video or a picture).")
        self._sync()

    def _sync(self):
        i = self.current()
        for k in ("use", "preview", "rename", "delete"):
            self.btns[k].setEnabled(i is not None)
        if i is not None:
            self.btns["use"].setEnabled(i.id != self.lib.active_id)
            self.btns["delete"].setEnabled(i.id != self.lib.active_id)
        self.btns["none"].setEnabled(bool(self.lib.active_id))

    def upload(self):
        f, _ = QFileDialog.getOpenFileName(self, "Choose an intro card", self.start_dir, FILE_FILTER)
        if not f:
            return
        stem = os.path.splitext(os.path.basename(f))[0]
        name, ok = QInputDialog.getText(self, "Name this intro", "Name:", text=stem)
        if not ok:
            return
        try:
            i = self.lib.add(f, name)
        except IntroError as e:
            self.msg.setText(str(e))
            QMessageBox.warning(self, "That intro can't be used", str(e))
            return
        if QMessageBox.question(self, "Use it now?", f"Added “{i.name}”. Use it for the next videos?") \
                == QMessageBox.StandardButton.Yes:
            self.lib.set_active(i.id)
        self.msg.setText(f"Added “{i.name}”.")
        self.refresh(i.id)

    def use(self):
        i = self.current()
        if i:
            self.lib.set_active(i.id)
            self.msg.setText(f"“{i.name}” will be used for the next video (prepared automatically at render).")
            self.refresh(i.id)

    def no_intro(self):
        self.lib.clear_active()
        self.msg.setText("Videos will have no intro card. Your intros are all still here.")
        self.refresh()

    def preview(self):
        i = self.current()
        if i:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.lib.path(i))))

    def rename(self):
        i = self.current()
        if i:
            name, ok = QInputDialog.getText(self, "Rename intro", "Name:", text=i.name)
            if ok and name.strip():
                self.lib.rename(i.id, name)
                self.refresh(i.id)

    def delete(self):
        i = self.current()
        if i and QMessageBox.question(self, "Delete intro", f"Delete “{i.name}”? This can't be undone.") \
                == QMessageBox.StandardButton.Yes:
            try:
                self.lib.remove(i.id)
            except IntroError as e:
                self.msg.setText(str(e))
            self.refresh()
