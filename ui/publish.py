"""Upload dialog: the title line Sam writes, privacy, schedule, and the one-time Google setup."""
from __future__ import annotations

import shutil
import traceback
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QVBoxLayout)

from core import youtube_publisher as yt

from . import theme

SETUP_STEPS = (
    "<b>One-time setup</b> (about 5 minutes)<br>"
    "1. Open <a href='https://console.cloud.google.com/'>console.cloud.google.com</a> and make a project<br>"
    "2. APIs &amp; Services → Library → enable <b>YouTube Data API v3</b><br>"
    "3. OAuth consent screen → External → add your Google account as a <b>test user</b><br>"
    "4. Credentials → OAuth client ID → <b>Desktop app</b> → Download JSON<br>"
    "5. Choose that file below"
)


class UploadWorker(QThread):
    progress = pyqtSignal(float)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, path: Path, meta: yt.VideoMeta, parent=None):
        super().__init__(parent)
        self.path, self.meta = path, meta
        import threading
        self.cancel = threading.Event()

    def run(self):
        try:
            session = yt.authorized_session(interactive=True)      # opens the browser the first time
            self.done.emit(yt.upload(self.path, self.meta, self.progress.emit, self.cancel, session=session))
        except InterruptedError as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(str(e) if isinstance(e, (yt.UploadError, ValueError)) else traceback.format_exc())


class PublishDialog(QDialog):
    def __init__(self, video: Path, hours: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Upload to YouTube")
        self.video, self.hours = Path(video), hours
        self.tpl = yt.load_template()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.setSpacing(12)
        title = QLabel("Upload to YouTube")
        title.setStyleSheet("font-size:18px; font-weight:500;")
        lay.addWidget(title)

        # --- setup (only until a client file exists)
        self.setup = QFrame()
        self.setup.setObjectName("panel")            # lilac rounded panel (theme)
        sl = QVBoxLayout(self.setup)
        sl.setContentsMargins(18, 14, 18, 14)
        steps = QLabel(SETUP_STEPS)
        steps.setTextFormat(Qt.TextFormat.RichText)
        steps.setOpenExternalLinks(True)
        steps.setWordWrap(False)            # one line per step: wrapped rich text mis-sizes in Qt
        steps.setMinimumHeight(steps.sizeHint().height())   # never squeeze the steps; the dialog grows
        sl.addWidget(steps)
        self.btn_secret = QPushButton("Choose the downloaded file")
        self.btn_secret.clicked.connect(self._choose_secret)
        self.btn_secret.setStyleSheet("background: white;")
        sl.addWidget(self.btn_secret, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.setup)

        # --- video details
        lay.addWidget(_muted("Title line (the part before “| Deep Sleep Hypnosis (Black Screen)”)"))
        self.hook = QLineEdit()
        self.hook.setPlaceholderText(self.tpl.get("default_hook", ""))
        self.hook.textChanged.connect(self._update)
        lay.addWidget(self.hook)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet("font-weight:500;")
        lay.addWidget(self.preview)
        row = QHBoxLayout()
        row.addWidget(_muted("Privacy"))
        self.privacy = QComboBox()
        for label, v in (("Private", "private"), ("Unlisted", "unlisted"), ("Public", "public")):
            self.privacy.addItem(label, v)
        self.privacy.setCurrentIndex(self.privacy.findData(self.tpl.get("privacy", "private")))
        row.addWidget(self.privacy)
        self.schedule = QCheckBox(f"Schedule to go live at {self.tpl.get('publish_time', '04:00')}")
        self.schedule.toggled.connect(self._update)
        row.addSpacing(12)
        row.addWidget(self.schedule)
        row.addStretch(1)
        lay.addLayout(row)
        self.note = _muted(yt.UNVERIFIED_NOTE)
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        self.error = QLabel("")
        self.error.setStyleSheet(f"color:{theme.RED};")
        self.error.setWordWrap(True)
        lay.addWidget(self.error)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Not now")
        cancel.clicked.connect(self.reject)
        self.ok = QPushButton("Upload")
        self.ok.setObjectName("primary")
        self.ok.clicked.connect(self._accept)
        btns.addWidget(cancel)
        btns.addWidget(self.ok)
        lay.addLayout(btns)
        self.meta: yt.VideoMeta | None = None
        self.resize(680, 560)
        self._update()

    def _choose_secret(self):
        f, _ = QFileDialog.getOpenFileName(self, "OAuth client file from Google Cloud", str(Path.home() / "Downloads"),
                                           "JSON (*.json)")
        if f:
            yt.DATA.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, yt.CLIENT_SECRET)
            self._update()

    def _build(self) -> yt.VideoMeta:
        return yt.build_meta(self.hook.text(), self.hours, self.tpl, schedule=self.schedule.isChecked(),
                             privacy=self.privacy.currentData())

    def _update(self):
        configured = yt.is_configured()
        self.setup.setVisible(not configured)
        self.btn_secret.setVisible(not configured)
        self.ok.setEnabled(configured)
        self.privacy.setEnabled(not self.schedule.isChecked())
        m = self._build()
        when = f" · goes live {m.publish_at:%a %d %b %H:%M}" if m.publish_at else ""
        self.preview.setText(f"{m.title}{when}")
        try:
            m.validate()
            self.error.setText("")
        except ValueError as e:
            self.error.setText(str(e))

    def _accept(self):
        try:
            self.meta = self._build()
            self.meta.validate()
        except ValueError as e:
            self.error.setText(str(e))
            return
        self.accept()


def _muted(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("muted")
    return lbl
