"""Canva controls for the upload/finish row."""
from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout

from core.canva import (CanvaExportWatcher, canva_export_folder, canva_template_url,
                        newest_png, remember_thumbnail, set_canva_export_folder,
                        set_canva_template_url, session_thumbnail)


class CanvaPanel(QFrame):
    """Open Canva and turn its newest PNG export into session thumbnail metadata."""

    thumbnail_selected = pyqtSignal(str)

    def __init__(self, settings: Any, parent=None, opener: Callable[[str], bool] | None = None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.settings = settings
        self._opener = opener or webbrowser.open
        self._session_dir: Path | None = None
        self._watcher = CanvaExportWatcher(canva_export_folder(settings))
        self._latest: Path | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(5)
        title = QLabel("Canva thumbnail")
        title.setObjectName("muted")
        outer.addWidget(title)

        template_row = QHBoxLayout()
        self.template = QLineEdit(canva_template_url(settings))
        self.template.setPlaceholderText("Paste Canva template link")
        self.template.setToolTip("Saved in Hush Studio settings")
        self.template.editingFinished.connect(self._save_template)
        template_row.addWidget(self.template, 1)
        self.btn_open = QPushButton("Open in Canva")
        self.btn_open.setObjectName("tool")
        self.btn_open.clicked.connect(self.open_canva)
        template_row.addWidget(self.btn_open)
        outer.addLayout(template_row)

        folder_row = QHBoxLayout()
        self.folder_label = QLabel()
        self.folder_label.setObjectName("muted")
        folder_row.addWidget(self.folder_label, 1)
        self.btn_folder = QPushButton("Export folder…")
        self.btn_folder.setObjectName("link")
        self.btn_folder.clicked.connect(self.choose_folder)
        folder_row.addWidget(self.btn_folder)
        outer.addLayout(folder_row)

        export_row = QHBoxLayout()
        self.status = QLabel("Waiting for a PNG export")
        self.status.setObjectName("muted")
        export_row.addWidget(self.status, 1)
        self.btn_use = QPushButton("Use newest PNG")
        self.btn_use.setObjectName("tool")
        self.btn_use.setEnabled(False)
        self.btn_use.clicked.connect(self.use_newest)
        export_row.addWidget(self.btn_use)
        outer.addLayout(export_row)

        self._refresh_folder_label()
        self._timer = QTimer(self)
        self._timer.setInterval(750)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    def set_session_dir(self, session_dir: str | Path | None) -> None:
        self._session_dir = Path(session_dir) if session_dir else None
        chosen = session_thumbnail(self._session_dir) if self._session_dir else None
        if chosen:
            self.status.setText(f"Selected: {chosen.name}")
        else:
            self.refresh()

    def _save_template(self) -> None:
        try:
            set_canva_template_url(self.settings, self.template.text())
            self.status.setText("Template link saved") if self.template.text().strip() else None
        except ValueError as exc:
            self.status.setText(str(exc))

    def open_canva(self) -> None:
        try:
            url = set_canva_template_url(self.settings, self.template.text())
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        if not url:
            self.status.setText("Add a Canva template link first")
            self.template.setFocus()
            return
        if self._opener(url):
            self.status.setText("Canva opened — export a PNG to the watched folder")

    def choose_folder(self) -> None:
        start = str(self._watcher.folder or Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Choose Canva export folder", start)
        if not folder:
            return
        try:
            saved = set_canva_export_folder(self.settings, folder)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self._watcher.set_folder(saved)
        self._refresh_folder_label()
        self.refresh()

    def _refresh_folder_label(self) -> None:
        folder = self._watcher.folder
        self.folder_label.setText(f"Watching: {folder}" if folder else "Choose where Canva exports PNGs")
        self.folder_label.setToolTip(str(folder) if folder else "")

    def refresh(self) -> None:
        latest = newest_png(self._watcher.folder)
        self._latest = latest
        self.btn_use.setEnabled(latest is not None and self._session_dir is not None)
        if latest is not None and not (self._session_dir and session_thumbnail(self._session_dir) == latest):
            self.status.setText(f"New export: {latest.name}")

    def use_newest(self) -> None:
        latest = self._latest or newest_png(self._watcher.folder)
        if latest is None or self._session_dir is None:
            return
        try:
            selected = remember_thumbnail(self._session_dir, latest)
        except (OSError, ValueError) as exc:
            self.status.setText(f"Couldn't use that export: {exc}")
            return
        self.status.setText(f"Selected: {selected.name}")
        self.btn_use.setEnabled(False)
        self.thumbnail_selected.emit(str(selected))
