"""Hush Studio: record (or import) a session, review the automatic edits, render 1–8 h."""
import sys

from PyQt6.QtWidgets import QApplication, QMessageBox

from core import ffmpeg_tools as ff
from core import log_setup
from core.app_info import APP_NAME
from ui.main_window import MainWindow
from ui import theme


def main() -> int:
    log_setup.setup_logging()
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
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
