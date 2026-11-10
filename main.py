"""Hush Studio: record (or import) a session, review the automatic edits, render 1–8 h."""
import sys

from PyQt6.QtWidgets import QApplication, QMessageBox

from core import ffmpeg_tools as ff
from core.app_info import APP_NAME
from ui.main_window import MainWindow
from ui import theme


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    theme.load_fonts()                               # bundled Montserrat (her site's font)
    app.setStyleSheet(theme.stylesheet())
    try:
        ff.check_ffmpeg_version()                    # FFmpeg >= 6, checked at startup
    except ff.FFmpegError as e:
        QMessageBox.critical(None, "FFmpeg needed", str(e))
        return 1
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
