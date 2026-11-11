"""Draw the app icon (purple circle, white crescent moon) → .work/hush.ico. Drawn in code, like
the editor's icons, so there is no third-party artwork in the installer. Needs PyQt6."""
import sys
from pathlib import Path

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPainterPath

PURPLE = QColor("#7A23A1")


def draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(PURPLE)
    p.drawEllipse(QRectF(0, 0, size, size))
    moon, bite = QPainterPath(), QPainterPath()
    moon.addEllipse(QRectF(size * 0.22, size * 0.20, size * 0.56, size * 0.60))
    bite.addEllipse(QRectF(size * 0.38, size * 0.12, size * 0.52, size * 0.56))
    p.setBrush(QColor("white"))
    p.drawPath(moon.subtracted(bite))
    p.end()
    return img


def main(out: str) -> int:
    QGuiApplication.instance() or QGuiApplication(["make_icon", "-platform", "offscreen"])
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not draw(256).save(str(path), "ICO"):
        print("could not write ICO (Qt image plugin 'qico' missing); building without an icon")
        return 0
    print(f"icon -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ".work/hush.ico"))
