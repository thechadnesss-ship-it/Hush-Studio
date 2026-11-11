"""Steps panel (T26): after an import the take opens raw, and Sam chooses what to apply.

    1  Import                      ✓ Done
    2  Remove background noise     [Apply] [Undo] [▶]
    3  Remove plane noise          [Apply] [Undo] [▶]
    4  Auto-edit pauses            [Apply] [Undo] [▶]
    5  Normalise                   [Apply] [Undo] [▶]
    6  Studio polish               [Apply] [Undo] [▶]
       [ Do all (recommended) ]

A view only: the editor dialog does the work (each step is one undoable change of the edit list,
core/editing.py) and calls `show_state()` after every change. Live recordings arrive with the
automatic steps already applied; the same rows then offer Undo.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from . import theme

# key, title, plain description (what it does, how long, what it leaves alone)
STEPS: list[tuple[str, str, str]] = [
    ("denoise", "Remove background noise",
     "Takes out hiss, hum and room noise and keeps her voice. About a minute on a long take."),
    ("planes", "Remove plane noise",
     "Finds planes and loud noises and cuts them out at her pauses. Each cut is marked ▼ on the waveform."),
    ("pauses", "Auto-edit pauses",
     "Her pause-marker word becomes a long silence, other pauses become clean silence, and repeated "
     "phrases are suggested as cuts for her to accept."),
    ("normalise", "Normalise",
     "Sets the whole take to the channel's level (−21 LUFS), so the waveform shows how it will sound."),
    ("polish", "Studio polish",
     "Optional: warmth, softer “s” sounds and gentle evening-out, added when the video is made."),
]
DO_ALL = ("denoise", "planes", "pauses", "normalise")     # today's automatic result (polish stays her choice)

QSS = f"""
QFrame#steps {{ background: {theme.LILAC_SOFT}; border-left: 1px solid {theme.BORDER}; }}
QFrame#steprow {{ background: {theme.BG}; border: 1px solid {theme.BORDER}; border-radius: 10px; }}
QLabel#steptitle {{ font-weight: 600; font-size: 13px; }}
QLabel#stepdesc {{ color: {theme.MUTED}; font-size: 11px; }}
QLabel#stepnum {{ color: {theme.ACCENT}; font-weight: 700; font-size: 13px; }}
QFrame#steprow QPushButton {{ min-height: 30px; max-height: 30px; border-radius: 15px; padding: 0px 12px;
    font-size: 12px; }}
QLabel#stepstate {{ font-size: 11px; }}
"""


class StepRow(QFrame):
    def __init__(self, num: int, key: str, title: str, desc: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.setObjectName("steprow")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(6)
        n = QLabel(str(num))
        n.setObjectName("stepnum")
        head.addWidget(n)
        t = QLabel(title)
        t.setObjectName("steptitle")
        t.setWordWrap(True)
        head.addWidget(t, 1)
        lay.addLayout(head)
        self.state = QLabel("")
        self.state.setObjectName("stepstate")
        self.desc = QLabel(desc)
        self.desc.setObjectName("stepdesc")
        self.desc.setWordWrap(True)
        lay.addWidget(self.desc)
        self.note = QLabel("")
        self.note.setObjectName("stepdesc")
        self.note.setWordWrap(True)
        self.note.hide()
        lay.addWidget(self.note)
        self.btns = QHBoxLayout()
        self.btns.setSpacing(6)
        self.apply = QPushButton("Apply")
        self.undo = QPushButton("Undo")
        self.preview = QPushButton("▶ Listen")
        for b in (self.apply, self.undo, self.preview):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setAutoDefault(False)
            self.btns.addWidget(b)
        self.apply.setObjectName("primary")
        self.preview.setToolTip("Listen from the cursor (20 s) with this step")
        self.btns.addStretch(1)
        self.btns.addWidget(self.state)
        lay.addLayout(self.btns)

    def show_state(self, done: bool | None, busy: bool, why_not: str = "", can_preview: bool = True) -> None:
        """done None = finished and fixed (Import)."""
        if done is None:
            self.state.setText("✓ Done")
            self.state.setStyleSheet(f"color:{theme.GREEN}; font-weight:600;")
            for b in (self.apply, self.undo, self.preview):
                b.hide()
            self.btns.insertStretch(0, 1)
            return
        self.state.setText("✓ Applied" if done else "Not applied")
        self.state.setStyleSheet(f"color:{theme.GREEN}; font-weight:600;" if done else f"color:{theme.MUTED};")
        self.apply.setEnabled(not done and not busy and not why_not)
        self.undo.setEnabled(done and not busy)
        self.preview.setEnabled(not busy and can_preview)
        self.note.setText(why_not)
        self.note.setVisible(bool(why_not))


class StepsPanel(QFrame):
    apply = pyqtSignal(str)
    undo = pyqtSignal(str)
    preview = pyqtSignal(str)
    do_all = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("steps")
        self.setStyleSheet(QSS)
        self.setFixedWidth(320)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("stepsinner")
        inner.setStyleSheet(f"QWidget#stepsinner {{ background: {theme.LILAC_SOFT}; }}")
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        head = QLabel("Steps")
        head.setStyleSheet("font-size: 16px; font-weight: 600;")
        lay.addWidget(head)
        self.intro = QLabel("Apply what this take needs. Each step can be undone on its own; raw.flac is never changed.")
        self.intro.setObjectName("stepdesc")
        self.intro.setWordWrap(True)
        lay.addWidget(self.intro)
        self.btn_all = QPushButton("Do all (recommended)")
        self.btn_all.setObjectName("primary")
        self.btn_all.setToolTip("Remove background noise, remove plane noise, auto-edit pauses and normalise, "
                                "one after the other (what the app used to do on import)")
        self.btn_all.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_all.setAutoDefault(False)
        self.btn_all.clicked.connect(self.do_all.emit)
        lay.addWidget(self.btn_all)
        self.import_row = StepRow(1, "import", "Import", "The take as recorded, nothing processed yet.")
        self.import_row.show_state(None, False)
        lay.addWidget(self.import_row)
        self.rows: dict[str, StepRow] = {}
        for i, (key, title, desc) in enumerate(STEPS, start=2):
            row = StepRow(i, key, title, desc)
            row.apply.clicked.connect(lambda _=False, k=key: self.apply.emit(k))
            row.undo.clicked.connect(lambda _=False, k=key: self.undo.emit(k))
            row.preview.clicked.connect(lambda _=False, k=key: self.preview.emit(k))
            self.rows[key] = row
            lay.addWidget(row)
        lay.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

    def show_state(self, state: dict[str, bool], busy: bool, why_not: dict[str, str] | None = None,
                   can_preview: dict[str, bool] | None = None, running: str | None = None) -> None:
        why_not = why_not or {}
        can_preview = can_preview or {}
        for key, row in self.rows.items():
            row.show_state(state.get(key, False), busy, why_not.get(key, ""), can_preview.get(key, True))
            if running == key:
                row.state.setText("Working…")
                row.state.setStyleSheet(f"color:{theme.ACCENT}; font-weight:600;")
        todo = [k for k in DO_ALL if not state.get(k) and not why_not.get(k)]
        self.btn_all.setEnabled(bool(todo) and not busy)
        self.btn_all.setText("Do all (recommended)" if todo else "All steps applied ✓")
