"""Takes screen (T10): every saved take with name, date, length and status; search, rename,
duplicate, delete, Show in Explorer, export/import (.hushtake), and reopen into review or render."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QAbstractItemView, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                             QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from core.renderer import fmt_hms
from core.takes import AUDIO_EXTENSIONS, EXPORT_EXT, Take, TakeLibrary


def _drop_paths(mime) -> list[Path]:
    if not mime.hasUrls():
        return []
    return [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()]


class _TakeTable(QTableWidget):
    """Table that accepts audio and .hushtake files dragged from Explorer."""

    files_dropped = pyqtSignal(list)

    def dragEnterEvent(self, event):
        if _drop_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = _drop_paths(event.mimeData())
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()


class TakesDialog(QDialog):
    """`result_take` / `result_action` ("review" | "render") are set when Sam reopens a take."""

    def __init__(self, library: TakeLibrary, parent=None, busy: bool = False):
        super().__init__(parent)
        self.lib = library
        self.result_take: Take | None = None
        self.result_action = ""
        self.setWindowTitle("Takes")
        self.resize(820, 520)
        self.setAcceptDrops(True)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search takes by name, date, notes or status")
        self.search.textChanged.connect(self.refresh)
        top.addWidget(self.search, 1)
        self.btn_import = QPushButton("Import .hushtake…")
        self.btn_import.setObjectName("link")
        self.btn_import.clicked.connect(self.import_take)
        top.addWidget(self.btn_import)
        lay.addLayout(top)

        self.table = _TakeTable(0, 4)
        self.table.setAcceptDrops(True)
        self.table.files_dropped.connect(self.import_paths)
        self.table.setHorizontalHeaderLabels(["Name", "Date", "Length", "Status"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in (1, 2, 3):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._selection)
        self.table.itemDoubleClicked.connect(lambda *_: self.reopen("review"))
        lay.addWidget(self.table, 1)
        self.empty = QLabel("")
        self.empty.setObjectName("muted")
        lay.addWidget(self.empty)

        row = QHBoxLayout()
        self.btns: dict[str, QPushButton] = {}
        for key, text, fn in [("review", "Open in editor", lambda: self.reopen("review")),
                              ("render", "Render", lambda: self.reopen("render")),
                              ("rename", "Rename", self.rename), ("dup", "Duplicate", self.duplicate),
                              ("explorer", "Show in Explorer", self.explorer), ("export", "Export…", self.export),
                              ("delete", "Delete", self.delete)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            self.btns[key] = b
            row.addWidget(b)
        self.btns["review"].setObjectName("primary")
        if busy:                                    # a recording / render is under way: managing is fine, reopening is not
            self.btns["review"].setEnabled(False)
            self.btns["render"].setEnabled(False)
        self._busy = busy
        lay.addLayout(row)
        self.refresh()

    # ------------------------------------------------------------------ list

    def refresh(self, *_):
        keep = self.current().id if self.current() else None
        self.takes = self.lib.list(self.search.text())
        self.table.setRowCount(len(self.takes))
        for r, t in enumerate(self.takes):
            vals = [t.name, t.created.strftime("%d %b %Y, %H:%M"), fmt_hms(t.length_s) if t.length_s else "–",
                    t.status.capitalize()]
            for c, v in enumerate(vals):
                self.table.setItem(r, c, QTableWidgetItem(v))
            if t.id == keep:
                self.table.selectRow(r)
        self.empty.setText("" if self.takes else ("No takes match." if self.search.text() else
                                                  "No takes yet. Record or import one and it appears here."))
        self._selection()

    def current(self) -> Take | None:
        r = self.table.currentRow() if hasattr(self, "table") else -1
        return self.takes[r] if 0 <= r < len(getattr(self, "takes", [])) else None

    def _selection(self):
        t = self.current()
        for k, b in self.btns.items():
            b.setEnabled(t is not None and not (self._busy and k in ("review", "render")))

    # ------------------------------------------------------------------ actions

    def reopen(self, action: str):
        t = self.current()
        if t is None or self._busy:
            return
        if not (t.dir / "raw.flac").exists() or not (t.dir / "clean.wav").exists():
            QMessageBox.information(self, "Takes", "This take has no cleaned audio yet, so it can't be opened.")
            return
        self.result_take, self.result_action = t, action
        self.accept()

    def rename(self):
        t = self.current()
        if t is None:
            return
        name, ok = QInputDialog.getText(self, "Rename take", "Name:", text=t.name)
        if ok and name.strip():
            self.lib.rename(t, name)
            self.refresh()

    def duplicate(self):
        t = self.current()
        if t is not None:
            self.lib.duplicate(t)
            self.refresh()

    def delete(self):
        t = self.current()
        if t is None:
            return
        extra = "\n\nIts rendered video will be deleted too." if t.rendered_files else ""
        if QMessageBox.question(self, "Delete take", f"Delete “{t.name}” for good? This can't be undone.{extra}",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.lib.delete(t)
            self.refresh()

    def explorer(self):
        t = self.current()
        if t is not None:
            self.lib.show_in_explorer(t)

    def export(self):
        t = self.current()
        if t is None:
            return
        filters = (f"Hush take (*{EXPORT_EXT});;WAV audio (*.wav);;FLAC audio (*.flac);;"
                   "MP3 audio (*.mp3);;Finished video (*.mp4)")
        f, _ = QFileDialog.getSaveFileName(self, "Export take", f"{t.name}{EXPORT_EXT}", filters)
        if f:
            try:
                self.lib.export_path(t, Path(f))
            except Exception as e:
                QMessageBox.warning(self, "Takes", str(e))

    def import_take(self):
        audio = " ".join(f"*{ext}" for ext in sorted(AUDIO_EXTENSIONS))
        filters = f"Takes (*{EXPORT_EXT} {audio});;Hush take (*{EXPORT_EXT});;Audio ({audio})"
        f, _ = QFileDialog.getOpenFileName(self, "Import a take", "", filters)
        if not f:
            return
        self.import_paths([Path(f)])

    def dragEnterEvent(self, event):
        if _drop_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = _drop_paths(event.mimeData())
        if paths:
            self.import_paths(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

    def import_paths(self, paths: list[Path]):
        errors = []
        for path in paths:
            try:
                self.lib.import_path(path)
            except Exception as e:
                errors.append(f"{path.name}: {e}")
        if errors:
            QMessageBox.warning(self, "Takes", "\n".join(errors))
        self.refresh()
