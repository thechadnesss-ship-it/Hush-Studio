"""Updates (T23): check at start-up and from Help → Check for updates…, offer What's new and
Update now, never while recording/rendering/uploading, quiet when offline, skip a version.

The network and installer work is in core/updater.py; this is the Qt side:
  UpdateController  owned by MainWindow: background check, the header "Update available" link,
                    skipped version and the "did the last update land?" note after a restart.
  UpdateDialog      What's new · Update now · Skip this version · Later; downloads with a
                    progress bar, checks the SHA-256, hands over to Setup and closes the app.
"""
from __future__ import annotations

import threading
import traceback
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton,
                             QTextBrowser, QVBoxLayout)

from core import updater
from core.app_info import APP_NAME, APP_VERSION
from core.log_setup import get_logger

SKIP_KEY = "update/skipped_version"
PENDING_KEY = "update/pending_version"
log = get_logger("updater")


class CheckWorker(QThread):
    done = pyqtSignal(object)          # Release | None
    failed = pyqtSignal(str)

    def run(self):
        try:
            self.done.emit(updater.check())
        except updater.UpdateError as e:
            self.failed.emit(str(e))
        except Exception:                                   # never let a check crash the app
            log.warning("update check crashed:\n%s", traceback.format_exc())
            self.failed.emit("The update check didn't work this time.")


class DownloadWorker(QThread):
    progress = pyqtSignal(float)
    done = pyqtSignal(object)          # Path of the verified installer
    failed = pyqtSignal(str)

    def __init__(self, release: updater.Release, folder: Path, parent=None):
        super().__init__(parent)
        self.release, self.folder = release, folder
        self.cancel = threading.Event()

    def run(self):
        try:
            self.done.emit(updater.download(self.release, self.folder, self.progress.emit, self.cancel))
        except updater.UpdateError as e:
            self.failed.emit(str(e))
        except Exception:
            log.warning("update download crashed:\n%s", traceback.format_exc())
            self.failed.emit("The download didn't work this time. Try again later.")


class UpdateDialog(QDialog):
    def __init__(self, release: updater.Release, controller: "UpdateController", parent=None):
        super().__init__(parent)
        self.release, self.ctl = release, controller
        self.worker: DownloadWorker | None = None
        self.setWindowTitle("Update available")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        head = QLabel(f"<b>{APP_NAME} {release.version}</b> is ready (you have {APP_VERSION}).")
        head.setWordWrap(True)
        lay.addWidget(head)
        sub = QLabel("What's new")
        sub.setObjectName("muted")
        lay.addWidget(sub)
        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        self.notes.setMarkdown(release.notes or "Small fixes and improvements.")
        self.notes.setMinimumHeight(180)
        lay.addWidget(self.notes, 1)
        keep = QLabel("Your takes, intros and settings are kept. The app closes, updates and opens again "
                      "by itself (about a minute).")
        keep.setObjectName("muted")
        keep.setWordWrap(True)
        lay.addWidget(keep)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.hide()
        lay.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.hide()
        lay.addWidget(self.bar)
        row = QHBoxLayout()
        self.btn_skip = QPushButton("Skip this version")
        self.btn_skip.setObjectName("link")
        self.btn_skip.clicked.connect(self.skip)
        row.addWidget(self.btn_skip)
        row.addStretch(1)
        self.btn_later = QPushButton("Later")
        self.btn_later.clicked.connect(self.reject)
        row.addWidget(self.btn_later)
        self.btn_update = QPushButton("Update now")
        self.btn_update.setObjectName("primary")
        self.btn_update.setDefault(True)
        self.btn_update.clicked.connect(self.update_now)
        row.addWidget(self.btn_update)
        lay.addLayout(row)

    def _say(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def skip(self):
        self.ctl.skip(self.release.version)
        self.reject()

    def update_now(self):
        why = self.ctl.blocker()
        if why:
            self._say(why)
            return
        if not self.ctl.can_install():
            # run from source: nothing to install over, so show the release page instead
            QDesktopServices.openUrl(QUrl(self.release.page_url))
            self.accept()
            return
        self._say("Downloading the update…")
        self.bar.setValue(0)
        self.bar.show()
        self.btn_update.setEnabled(False)
        self.btn_skip.setEnabled(False)
        self.btn_later.setText("Cancel")
        self.worker = DownloadWorker(self.release, self.ctl.download_dir, self)
        self.worker.progress.connect(lambda f: self.bar.setValue(int(f * 1000)))
        self.worker.done.connect(self._downloaded)
        self.worker.failed.connect(self._failed)
        self.worker.start()

    def _downloaded(self, setup: Path):
        why = self.ctl.blocker()                # she may have started something meanwhile
        if why:
            self._failed(why + " The update is downloaded; choose Update now again when you're done.")
            return
        self._say("Installing… the app will open again by itself.")
        self.ctl.install(setup, self.release.version)
        self.accept()

    def _failed(self, msg: str):
        log.info("update not installed: %s", msg)
        self.bar.hide()
        self._say(msg)
        self.btn_update.setEnabled(True)
        self.btn_skip.setEnabled(True)
        self.btn_later.setText("Later")

    def reject(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel.set()
            self.worker.wait(5000)
        super().reject()


class UpdateController(QObject):
    """One per main window. `window.update_blocker()` says why updating must wait (or None)."""
    available = pyqtSignal(object)      # Release offered (not skipped)

    def __init__(self, window, settings, download_dir: Path, header_button: QPushButton | None = None):
        super().__init__(window)
        self.window, self.settings, self.download_dir = window, settings, download_dir
        self.header_button = header_button
        self.release: updater.Release | None = None
        self._worker: CheckWorker | None = None
        self._manual = False
        self.dialog: UpdateDialog | None = None
        self.quit_app = QApplication.quit          # replaced in tests
        self.launch = updater.launch_installer     # replaced in tests
        if header_button is not None:
            header_button.hide()
            header_button.clicked.connect(self.show_dialog)

    # ---- state the dialog asks about
    def blocker(self) -> str | None:
        fn = getattr(self.window, "update_blocker", None)
        return fn() if fn else None

    def can_install(self) -> bool:
        return updater.relaunch_target() is not None

    def skip(self, version: str) -> None:
        self.settings.setValue(SKIP_KEY, version)
        if self.header_button is not None:
            self.header_button.hide()

    def skipped(self) -> str:
        return str(self.settings.value(SKIP_KEY, "") or "")

    # ---- checking
    def check(self, manual: bool = False) -> None:
        """Ask GitHub in the background. Automatic checks are silent unless there's an update."""
        if self._worker is not None and self._worker.isRunning():
            self._manual = self._manual or manual
            return
        self._manual = manual
        self._worker = CheckWorker(self)
        self._worker.done.connect(self._checked)
        self._worker.failed.connect(self._check_failed)
        self._worker.start()

    def _checked(self, rel: updater.Release | None) -> None:
        manual = self._manual
        if rel is None:
            log.info("update check: up to date (%s)", APP_VERSION)
            if manual:
                QMessageBox.information(self.window, "Check for updates",
                                        f"You have the newest version ({APP_VERSION}).")
            return
        log.info("update check: %s available (have %s, skipped %r)", rel.version, APP_VERSION, self.skipped())
        self.release = rel
        if not manual and rel.version == self.skipped():
            return
        if self.header_button is not None:
            self.header_button.setText(f"Update to {rel.version}")
            self.header_button.show()
        self.available.emit(rel)
        # Don't pop a dialog over a recording/render or another dialog (first-run mic check):
        # the header link waits for her instead.
        if manual or (not self.blocker() and QApplication.activeModalWidget() is None):
            self.show_dialog()

    def _check_failed(self, msg: str) -> None:
        log.info("update check failed: %s", msg)        # offline start: log only, nothing shown
        if self._manual:
            QMessageBox.information(self.window, "Check for updates", msg)

    def show_dialog(self) -> None:
        if self.release is None or (self.dialog is not None and self.dialog.isVisible()):
            return
        self.dialog = UpdateDialog(self.release, self, self.window)
        self.dialog.open()

    # ---- installing
    def install(self, setup: Path, version: str) -> None:
        self.settings.setValue(PENDING_KEY, version)
        self.settings.sync()
        log.info("handing over to %s (-> %s)", setup.name, version)
        self.launch(setup, relaunch=updater.relaunch_target())
        self.window.close()
        self.quit_app()

    def after_start(self) -> str | None:
        """After an update restart: a one-line note for the window (and tidy the download)."""
        pending = str(self.settings.value(PENDING_KEY, "") or "")
        if not pending:
            updater.clean_downloads(self.download_dir)
            return None
        self.settings.remove(PENDING_KEY)
        if not updater.is_newer(pending, APP_VERSION):          # we're on it (or newer)
            log.info("updated to %s", APP_VERSION)
            updater.clean_downloads(self.download_dir)
            return f"Updated to {APP_NAME} {APP_VERSION}. Your takes and settings are all here."
        code = updater.last_install_exit_code(self.download_dir)
        log.warning("update to %s didn't land (still %s, setup exit code %s, log %s)",
                    pending, APP_VERSION, code, self.download_dir / "install.log")
        return f"The update to {pending} didn't finish, so you're still on {APP_VERSION}. Nothing was lost."
