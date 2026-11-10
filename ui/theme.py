"""Look & feel: Sam's brand (samkatz.store) in a calm, Riverside-style minimalist layout.

Measured from her store theme (2026-11-09): white background, black text, purple #7A23A1
buttons, soft lilac section colours (#D2BFE1, #CEA1D8, #DCA8DA, #CEADD3), Montserrat, pill buttons.
Green / amber / red stay for the plane light only, because they carry meaning.
"""
from pathlib import Path

BG = "#FFFFFF"
TEXT = "#141016"
MUTED = "#7A7183"
ACCENT = "#7A23A1"           # her purple
ACCENT_HOVER = "#8E35B6"
LILAC = "#D2BFE1"            # her section lilac
LILAC_SOFT = "#F5EFF9"       # very light lilac surface
BORDER = "#E9E1EF"
REC_RED = "#E0455A"
GREEN = "#2FA36B"
AMBER = "#E39B2D"
RED = "#E0455A"

# kept names used elsewhere
SURFACE = LILAC_SOFT
SURFACE_2 = BORDER

LIGHT_COLORS = {"green": GREEN, "amber": AMBER, "red": RED, "clear": GREEN, "off": MUTED, "idle": MUTED}

FONT_FILE = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Montserrat.ttf"
FONT_FAMILY = "Montserrat"


def load_fonts() -> str:
    """Register the bundled Montserrat (SIL OFL); fall back to Segoe UI."""
    global FONT_FAMILY
    try:
        from PyQt6.QtGui import QFontDatabase
        if FONT_FILE.exists():
            fid = QFontDatabase.addApplicationFont(str(FONT_FILE))
            fams = QFontDatabase.applicationFontFamilies(fid)
            if fams:
                FONT_FAMILY = fams[0]
                return FONT_FAMILY
    except Exception:
        pass
    FONT_FAMILY = "Segoe UI"
    return FONT_FAMILY


def stylesheet() -> str:
    f = FONT_FAMILY
    return f"""
QWidget {{ background: {BG}; color: {TEXT}; font-family: "{f}", "Segoe UI", sans-serif; font-size: 14px; }}
QLabel#brand {{ color: {ACCENT}; font-size: 18px; font-weight: 600; letter-spacing: 0.5px; }}
QLabel#timer {{ font-size: 76px; font-weight: 200; letter-spacing: 3px; }}
QLabel#plan {{ color: {MUTED}; font-size: 14px; }}
QLabel#message, QLabel#muted {{ color: {MUTED}; }}
QLabel#bubble {{ color: {MUTED}; font-size: 14px; }}
QPushButton {{ background: {BG}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 19px;
    min-height: 38px; max-height: 38px; padding: 0px 22px; font-weight: 500; }}
QPushButton:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
QPushButton:disabled {{ color: #C4BCCB; border-color: #F0EBF3; }}
QPushButton#primary {{ background: {ACCENT}; color: white; border: none; }}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; color: white; }}
QPushButton#primary:disabled {{ background: {LILAC}; color: white; }}
QPushButton#link {{ border: none; color: {ACCENT}; padding: 0px 6px; background: transparent; min-height: 24px; }}
QPushButton#link:hover {{ text-decoration: underline; }}
QComboBox, QDoubleSpinBox, QSpinBox, QLineEdit {{ background: {BG}; border: 1px solid {BORDER};
    border-radius: 16px; padding: 6px 14px; }}
QComboBox:hover {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button, QSpinBox::up-button, QSpinBox::down-button {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{ border: 1px solid {BORDER}; selection-background-color: {LILAC_SOFT};
    selection-color: {ACCENT}; }}
QFrame#panel {{ background: {LILAC_SOFT}; border-radius: 18px; }}
QFrame#panel QWidget {{ background: transparent; }}
QTableWidget {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 12px; gridline-color: {BORDER}; }}
QTableWidget::item:selected {{ background: {LILAC_SOFT}; color: {TEXT}; }}
QHeaderView::section {{ background: {BG}; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER};
    padding: 8px; font-weight: 500; }}
QProgressBar {{ background: {LILAC_SOFT}; border: none; border-radius: 3px; max-height: 6px; color: transparent; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 1px solid {LILAC}; background: {BG}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QDialog {{ background: {BG}; }}
"""


STYLESHEET = stylesheet()
