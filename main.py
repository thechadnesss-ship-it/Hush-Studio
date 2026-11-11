"""Hush Studio: record (or import) a session, review the automatic edits, render 1–8 h."""
import sys
import threading

from PyQt6.QtCore import QObject, QSettings, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QMessageBox

from core import ffmpeg_tools as ff
from core import log_setup
from core import mic_check
from core.app_info import APP_NAME, SETTINGS_ORG
from ui.main_window import MainWindow
from ui import theme

# The installer (build/hush_studio.iss, AppMutex) looks for this to ask Sam to close the app
# before upgrading or uninstalling.
APP_MUTEX = "HushStudioAppMutex"
MIC_OK_KEY = "setup/mic_ok"


def _hold_app_mutex():
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        return ctypes.windll.kernel32.CreateMutexW(None, False, APP_MUTEX)
    except Exception:
        return None


class _MicCheckRunner(QObject):
    """Listens on a plain thread (half a second of mic) and reports back on the GUI thread."""
    finished = pyqtSignal(object)

    def start(self) -> None:
        threading.Thread(target=lambda: self.finished.emit(mic_check.check_microphone()), daemon=True).start()


def first_run_mic_check(window) -> None:
    """Until the mic has worked once, check it at start-up and explain any Windows block."""
    settings = QSettings(SETTINGS_ORG, APP_NAME)
    if settings.value(MIC_OK_KEY, False, type=bool):
        return
    log = log_setup.get_logger("capture")
    runner = _MicCheckRunner(window)

    def done(res: mic_check.MicCheckResult) -> None:
        log.info("first-run mic check: ok=%s problem=%s device=%s peak=%s detail=%s",
                 res.ok, res.problem, res.device, res.peak_dbfs, res.detail)
        if res.ok:
            settings.setValue(MIC_OK_KEY, True)
            return
        box = QMessageBox(window)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(APP_NAME)
        box.setText(res.title)
        box.setInformativeText(res.advice)
        open_btn = box.addButton("Open Windows settings", QMessageBox.ButtonRole.ActionRole)
        again_btn = box.addButton("Check again", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl(res.settings_uri))
        elif box.clickedButton() is again_btn:
            runner.start()

    runner.finished.connect(done)
    runner.start()


def main() -> int:
    if "--self-test" in sys.argv:                    # installer check: HushStudio.exe --self-test report.json
        from core import selftest
        i = sys.argv.index("--self-test")
        return selftest.main(sys.argv[i + 1:i + 2])
    log_setup.setup_logging()
    mutex = _hold_app_mutex()                        # noqa: F841 (held for the app's lifetime)
    app = QApplication(sys.argv)
    log_setup.install_crash_handler(lambda msg: QMessageBox.critical(None, APP_NAME, msg))
    app.setApplicationName(APP_NAME)
    theme.load_fonts()                               # bundled Montserrat (her site's font)
    app.setStyleSheet(theme.stylesheet())
    try:
        ffmpeg_version = ff.check_ffmpeg_version()   # FFmpeg >= 6, checked at startup
    except ff.FFmpegError as e:
        log_setup.get_logger("app").error("FFmpeg check failed: %s", e)
        QMessageBox.critical(None, "FFmpeg needed", str(e))
        return 1
    log_setup.log_startup(ffmpeg_version)
    w = MainWindow()
    w.show()
    QTimer.singleShot(400, lambda: first_run_mic_check(w))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
