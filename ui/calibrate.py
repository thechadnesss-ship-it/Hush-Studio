"""Check mic (T27): choose the mic, 5 s of quiet, read one sentence, get plain advice + a stored gain.

    setup page  : mic picker · setup checks (privacy switches, laptop vs USB mic, Krisp, 48 kHz,
                  speakers vs headphones) · "Start"
    measure page: "Stay quiet…" countdown, then the sentence to read · live meter with target zone
    result page : advice in plain words · numbers · "Try again" / "Save"

Audio comes from an `AudioSource` (core.capture.MicSource by default; tests pass an ArraySource).
Its callback only enqueues; a GUI timer drains the queue. Phases are counted in samples, not wall
time, so a source that delivers faster than real time measures the same thing.
"""
from __future__ import annotations

import queue
from typing import Callable

import numpy as np
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QStackedWidget,
                             QVBoxLayout, QWidget)

from core import calibration as cal
from core import mic_check
from core.log_setup import get_logger

from . import theme
from .widgets import VUMeter

log = get_logger("capture")

DEFAULT_MIC = "Default microphone"
_LEVEL_COL = {"ok": theme.GREEN, "info": theme.MUTED, "warn": theme.AMBER, "bad": theme.RED}
_LEVEL_MARK = {"ok": "✓", "info": "•", "warn": "!", "bad": "✕"}


# =========================================================================== system helpers

def list_mics() -> list[tuple[str, object]]:
    """(label, device) for the mic picker: the Windows default plus every WASAPI input."""
    items: list[tuple[str, object]] = [(DEFAULT_MIC, None)]
    try:
        import sounddevice as sd
        seen = set()
        for i, d in enumerate(sd.query_devices()):
            api = sd.query_hostapis(d["hostapi"])["name"]
            if d["max_input_channels"] > 0 and api == "Windows WASAPI" and d["name"] not in seen:
                seen.add(d["name"])
                items.append((d["name"], i))
    except Exception:
        pass
    return items


def resolve_device_name(device) -> str:
    """The real name of the mic that `device` (None = Windows default) opens; '' if unknown."""
    try:
        import sounddevice as sd
        info = sd.query_devices(kind="input") if device is None else sd.query_devices(device)
        return str(info["name"])
    except Exception:
        return ""


def system_snapshot(device, label: str = "") -> cal.DeviceSnapshot:
    """What Windows reports about the chosen mic and the playback device."""
    name = resolve_device_name(device)
    snap = cal.DeviceSnapshot(chosen=name or label, using_default=device is None, found=bool(name))
    try:
        import sounddevice as sd
        snap.inputs = [n for n, d in list_mics() if d is not None]
        snap.default_input = resolve_device_name(None)
        info = sd.query_devices(kind="input") if device is None else sd.query_devices(device)
        snap.default_rate = float(info.get("default_samplerate") or 0.0)
        try:
            sd.check_input_settings(device=device, samplerate=cal.SR, channels=1, dtype="float32")
        except Exception:
            snap.rate_ok = False
        snap.output = str(sd.query_devices(kind="output")["name"])
    except Exception:
        pass
    return snap


def privacy_advice(read=None) -> list[cal.Advice]:
    problem = mic_check.privacy_problem(read) if read else mic_check.privacy_problem()
    if not problem:
        return []
    title, text = mic_check.MESSAGES[problem]
    return [cal.Advice("privacy_" + problem, "bad", title, text)]


def advice_widget(items: list[cal.Advice]) -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8)
    for a in items:
        row = QHBoxLayout()
        mark = QLabel(_LEVEL_MARK[a.level])
        mark.setFixedWidth(18)
        mark.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        mark.setStyleSheet(f"color:{_LEVEL_COL[a.level]}; font-weight:700;")
        row.addWidget(mark)
        col = QVBoxLayout()
        col.setSpacing(2)
        t = QLabel(a.title)
        t.setWordWrap(True)
        t.setStyleSheet("font-weight:600;")
        col.addWidget(t)
        if a.text:
            d = QLabel(a.text)
            d.setWordWrap(True)
            d.setObjectName("muted")
            col.addWidget(d)
        row.addLayout(col, 1)
        lay.addLayout(row)
    return w


# =========================================================================== dialog

class CalibrateDialog(QDialog):
    """`mics`: (label, device) list; `current`: the label to preselect.
    On Save: `self.result` (Calibration, already stored) and `self.mic_label`."""

    def __init__(self, store: cal.CalibrationStore, mics: list[tuple[str, object]] | None = None,
                 current: str = "", parent=None, source_factory: Callable | None = None,
                 snapshot: Callable[[object, str], cal.DeviceSnapshot] = system_snapshot,
                 privacy: Callable[[], list[cal.Advice]] = privacy_advice,
                 quiet_s: float = cal.QUIET_S, read_s: float = cal.READ_S, first_run: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Check your microphone")
        self.setMinimumWidth(560)
        self.store = store
        self.snapshot_fn = snapshot
        self.privacy_fn = privacy
        self.source_factory = source_factory or _mic_source
        self.quiet_n, self.read_n = int(quiet_s * cal.SR), int(read_s * cal.SR)
        self.result: cal.Calibration | None = None
        self.result_cal: cal.Calibration | None = None
        self.advice: list[cal.Advice] = []
        self.mic_label = current
        self.device_name = ""
        self.source = None
        self.q: queue.Queue[np.ndarray] = queue.Queue()
        self.phase = "setup"                         # setup | quiet | read | result
        self._quiet: list[np.ndarray] = []
        self._read: list[np.ndarray] = []
        self._quiet_len = self._read_len = 0
        self._recent_peaks: list[float] = []
        self.timer = QTimer(self, interval=50, timeout=self._drain)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 24)
        lay.setSpacing(12)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack)

        # ---- page 0: setup
        p0 = QWidget()
        l0 = QVBoxLayout(p0)
        l0.setContentsMargins(0, 0, 0, 0)
        l0.setSpacing(12)
        l0.addWidget(_title("Check your microphone"))
        intro = QLabel(("Let's set up your mic before your first recording. " if first_run else "")
                       + "It takes under a minute: 5 seconds of quiet, then you read one sentence. "
                         "Hush Studio remembers the result for this microphone.")
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        l0.addWidget(intro)
        row = QHBoxLayout()
        lbl = QLabel("Microphone")
        lbl.setObjectName("muted")
        row.addWidget(lbl)
        self.mic = QComboBox()
        for label, dev in (mics if mics is not None else list_mics()):
            self.mic.addItem(label, dev)
        k = self.mic.findText(current) if current else -1
        self.mic.setCurrentIndex(max(0, k))
        row.addWidget(self.mic, 1)
        l0.addLayout(row)
        self.setup_box = QVBoxLayout()
        l0.addLayout(self.setup_box)
        self.setup_view: QWidget | None = None
        l0.addStretch(1)
        b0 = QHBoxLayout()
        b0.addStretch(1)
        self.btn_cancel = QPushButton("Not now")
        self.btn_cancel.clicked.connect(self.reject)
        b0.addWidget(self.btn_cancel)
        self.btn_start = QPushButton("Start")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self.start)
        b0.addWidget(self.btn_start)
        l0.addLayout(b0)
        self.stack.addWidget(p0)

        # ---- page 1: measure
        p1 = QWidget()
        l1 = QVBoxLayout(p1)
        l1.setContentsMargins(0, 0, 0, 0)
        l1.setSpacing(14)
        self.step_title = _title("Stay quiet for 5 seconds")
        l1.addWidget(self.step_title)
        self.step_text = QLabel("")
        self.step_text.setWordWrap(True)
        self.step_text.setObjectName("muted")
        l1.addWidget(self.step_text)
        self.sentence = QLabel(cal.READ_SENTENCE)
        self.sentence.setWordWrap(True)
        self.sentence.setStyleSheet(f"font-size:19px; font-weight:400; background:{theme.LILAC_SOFT};"
                                    "border-radius:14px; padding:16px;")
        l1.addWidget(self.sentence)
        self.meter = VUMeter()
        self.meter.setFixedHeight(14)
        self.meter.set_zone(cal.ZONE)
        l1.addWidget(self.meter)
        scale = QHBoxLayout()
        for t in ("−60 dB", "", "target", "0 dB"):
            s = QLabel(t)
            s.setObjectName("muted")
            s.setStyleSheet("font-size:11px;")
            scale.addWidget(s, 1 if t else 3)
        l1.addLayout(scale)
        self.hint = QLabel("")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.hint.setStyleSheet("font-weight:600;")
        l1.addWidget(self.hint)
        self.countdown = QLabel("")
        self.countdown.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.countdown.setObjectName("muted")
        l1.addWidget(self.countdown)
        l1.addStretch(1)
        b1 = QHBoxLayout()
        b1.addStretch(1)
        self.btn_stop = QPushButton("Cancel")
        self.btn_stop.clicked.connect(self._cancel_measure)
        b1.addWidget(self.btn_stop)
        self.btn_done = QPushButton("I've finished reading")
        self.btn_done.setObjectName("primary")
        self.btn_done.clicked.connect(self._finish_reading)
        b1.addWidget(self.btn_done)
        l1.addLayout(b1)
        self.stack.addWidget(p1)

        # ---- page 2: result
        p2 = QWidget()
        l2 = QVBoxLayout(p2)
        l2.setContentsMargins(0, 0, 0, 0)
        l2.setSpacing(12)
        self.result_title = _title("Your microphone")
        l2.addWidget(self.result_title)
        self.result_box = QVBoxLayout()
        l2.addLayout(self.result_box)
        self.result_view: QWidget | None = None
        self.numbers = QLabel("")
        self.numbers.setObjectName("muted")
        self.numbers.setWordWrap(True)
        l2.addWidget(self.numbers)
        l2.addStretch(1)
        b2 = QHBoxLayout()
        b2.addStretch(1)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.reject)
        b2.addWidget(self.btn_close)
        self.btn_again = QPushButton("Try again")
        self.btn_again.clicked.connect(self.restart)
        b2.addWidget(self.btn_again)
        self.btn_save = QPushButton("Save")
        self.btn_save.setObjectName("primary")
        self.btn_save.clicked.connect(self.save)
        b2.addWidget(self.btn_save)
        l2.addLayout(b2)
        self.stack.addWidget(p2)

        self.mic.currentIndexChanged.connect(self.refresh_setup)
        self.refresh_setup()

    # ------------------------------------------------------------------ setup

    def refresh_setup(self):
        dev, label = self.mic.currentData(), self.mic.currentText()
        snap = self.snapshot_fn(dev, label)
        self.device_name = snap.chosen or label
        items = self.privacy_fn() + cal.setup_advice(snap)
        if any(a.code.startswith("privacy_") for a in items):
            items = [a for a in items if a.code != "setup_ok"]
        self.setup_items = items
        if self.setup_view is not None:
            self.setup_view.setParent(None)
        self.setup_view = advice_widget(items)
        self.setup_box.addWidget(self.setup_view)
        # nothing to measure without a mic, or while Windows blocks it
        self.btn_start.setEnabled(not any(a.code == "no_device" or a.code.startswith("privacy_") for a in items))
        prev = self.store.get(self.device_name)
        self.btn_start.setText("Check again" if prev else "Start")
        self.btn_start.setToolTip(prev.summary() if prev else "")

    # ------------------------------------------------------------------ measuring

    def start(self):
        self.mic_label = self.mic.currentText()
        self._quiet, self._read, self._quiet_len, self._read_len = [], [], 0, 0
        self._recent_peaks = []
        self.q = queue.Queue()
        self.meter.reset()
        self._enter("quiet")
        self.stack.setCurrentIndex(1)
        try:
            self.source = self.source_factory(self.mic.currentData())
            self.source.start(self.q.put)
        except Exception as e:
            log.warning("calibration: couldn't open %s: %s", self.device_name, e)
            self.source = None
            title, text = mic_check.MESSAGES["open_failed"]
            bad = [cal.Advice("open_failed", "bad", title, text)]
            if any(a.code == "rate" for a in getattr(self, "setup_items", [])):
                bad = [a for a in self.setup_items if a.code == "rate"] + bad
            self._show_result(None, bad)
            return
        self.timer.start()

    def _enter(self, phase: str):
        self.phase = phase
        quiet = phase == "quiet"
        self.step_title.setText("Stay quiet for 5 seconds" if quiet else "Now read this at your normal voice")
        self.step_text.setText("Sit where you'll record and don't speak: we're measuring the room."
                               if quiet else
                               "Speak the way you do in a session, at your usual distance from the mic.")
        self.sentence.setVisible(not quiet)
        self.btn_done.setVisible(not quiet)
        self.btn_done.setEnabled(False)
        self.hint.setText("")

    def _drain(self):
        got = []
        while True:
            try:
                got.append(np.asarray(self.q.get_nowait(), np.float32).reshape(-1))
            except queue.Empty:
                break
        for b in got:
            self._take(b)
            if self.phase not in ("quiet", "read"):
                return
        if got:
            x = np.concatenate(got)
            rms = cal.db(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
            peak = cal.db(np.abs(x).max(initial=0.0))
            self.meter.set_levels(rms, peak, clipped=peak >= -0.01)
            if self.phase == "read":
                self._live_hint(peak)
        self._update_countdown()

    def _take(self, b: np.ndarray):
        if self.phase == "quiet":
            need = self.quiet_n - self._quiet_len
            self._quiet.append(b[:need])
            self._quiet_len += min(need, len(b))
            if self._quiet_len >= self.quiet_n:
                self._enter("read")
                if len(b) > need:
                    self._take(b[need:])
        elif self.phase == "read":
            need = self.read_n - self._read_len
            self._read.append(b[:need])
            self._read_len += min(need, len(b))
            if self._read_len >= 4 * cal.SR:
                self.btn_done.setEnabled(True)
            if self._read_len >= self.read_n:
                self._finish_reading()

    def _live_hint(self, peak: float):
        self._recent_peaks = (self._recent_peaks + [peak])[-40:]            # ~2 s
        top = max(self._recent_peaks)
        if top >= -0.5:
            self.hint.setText("Too loud!")
            self.hint.setStyleSheet(f"font-weight:600; color:{theme.RED};")
        elif top < cal.TARGET_PEAK_DB - cal.STRONG_BOOST_DB:
            self.hint.setText("Coming in quiet")
            self.hint.setStyleSheet(f"font-weight:600; color:{theme.AMBER};")
        else:
            self.hint.setText("Good level")
            self.hint.setStyleSheet(f"font-weight:600; color:{theme.GREEN};")

    def _update_countdown(self):
        if self.phase == "quiet":
            left = (self.quiet_n - self._quiet_len) / cal.SR
            self.countdown.setText(f"{left:.0f} s")
        elif self.phase == "read":
            left = (self.read_n - self._read_len) / cal.SR
            self.countdown.setText(f"{left:.0f} s left")

    def _stop_source(self):
        self.timer.stop()
        if self.source is not None:
            try:
                self.source.stop()
            except Exception:
                pass
            self.source = None

    def _cancel_measure(self):
        self._stop_source()
        self.phase = "setup"
        self.stack.setCurrentIndex(0)

    def _finish_reading(self):
        if self.phase != "read":
            return
        self.phase = "result"
        self._stop_source()
        quiet = np.concatenate(self._quiet) if self._quiet else np.zeros(0, np.float32)
        read = np.concatenate(self._read) if self._read else np.zeros(0, np.float32)
        c, adv = cal.calibrate(self.device_name, quiet, read)
        log.info("calibration %s: floor %.1f dBFS%s, speech rms %.1f peak %.1f dBFS, gain %+.1f dB, advice %s",
                 self.device_name, c.floor_db, " (digital silence)" if c.digital_silence else "",
                 c.speech_rms_db, c.speech_peak_db, c.gain_db, c.advice)
        self._show_result(c, adv)

    def _show_result(self, c: cal.Calibration | None, adv: list[cal.Advice]):
        self.phase = "result"
        self.result_cal, self.advice = c, adv
        if self.result_view is not None:
            self.result_view.setParent(None)
        self.result_view = advice_widget(adv)
        self.result_box.addWidget(self.result_view)
        if c is not None:
            self.numbers.setText(
                f"Room {c.floor_db:.0f} dBFS · your voice {c.speech_rms_db:.0f} dBFS, peaks {c.speech_peak_db:.0f} dBFS"
                + (f" · boost {c.gain_db:+.1f} dB" if c.gain_db else "")
                + f" → peaks about {c.expected_peak_db:.0f} dBFS (target {cal.ZONE[0]:.0f} to {cal.ZONE[1]:.0f})")
        else:
            self.numbers.setText("")
        blocking = {"silent", "no_speech", "clipping", "open_failed", "rate"}
        usable = c is not None and not any(a.code in blocking for a in adv)
        self.btn_save.setEnabled(usable)
        self.btn_save.setText("Save anyway" if usable and any(a.level == "bad" for a in adv) else "Save")
        bad = any(a.level == "bad" for a in adv)
        self.result_title.setText("Please fix this and try again" if bad else "All set" if
                                  all(a.level in ("ok", "info") for a in adv) else "Nearly there")
        self.stack.setCurrentIndex(2)

    def restart(self):
        self.stack.setCurrentIndex(0)
        self.phase = "setup"
        self.refresh_setup()

    def save(self):
        if self.result_cal is None:
            return
        self.store.put(self.result_cal)
        self.result = self.result_cal
        self.accept()

    def done(self, r):
        self._stop_source()
        super().done(r)


def _title(text: str) -> QLabel:
    t = QLabel(text)
    t.setStyleSheet("font-size:18px; font-weight:500;")
    return t


def _mic_source(device):
    from core.capture import MicSource
    return MicSource(device)
