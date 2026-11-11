"""T27: mic calibration: level maths, plain advice, setup checks, per-device storage, the
calibrated gain in the recorder, the live level warning and the Check mic dialog."""
import json
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["HUSH_NO_PLANNER"] = "1"
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

import numpy as np
import pytest
import soundfile as sf

from core import calibration as cal
from core.capture import ArraySource, Recorder
from core.session import Session
from synth import SR, room_tone, speech_take

SHOTS = __import__("pathlib").Path(__file__).resolve().parent.parent / ".work"


def voice(dur: float, peak_db: float, seed: int = 2, floor_db: float = -70.0) -> np.ndarray:
    """Synthetic voice over room tone, scaled so the voice peaks at `peak_db`; the room tone is
    scaled with it (a mic level change moves both)."""
    x, _ = speech_take(dur, -24.0, seed=seed)
    g = 10 ** ((peak_db - cal.db(np.abs(x).max())) / 20)
    return (x * g).astype(np.float32)


def quiet(peak_db: float, seed: int = 5) -> np.ndarray:
    """The room recorded through the same mic setting as `voice(…, peak_db)`: 40 dB under the
    voice peak, like the USB mic here (room ≈ −56 dBFS, voice peaks ≈ −16 dBFS before make-up)."""
    return room_tone(cal.QUIET_S, peak_db - 40.0, seed=seed)


class DictSettings:
    def __init__(self):
        self.d = {}

    def value(self, k, default=None):
        return self.d.get(k, default)

    def setValue(self, k, v):
        self.d[k] = v

    def remove(self, k):
        self.d.pop(k, None)


# =========================================================================== level maths

def test_floor_and_speech_are_measured():
    f = cal.measure_floor(room_tone(5.0, -56.0, seed=1))
    assert -58 < f.rms_db < -54 and not f.digital_silence
    s = cal.measure_speech(voice(12.0, -20.0), f.rms_db)
    assert s.peak_db == pytest.approx(-20.0, abs=0.1)
    assert s.active_s > 2.0 and s.clipped == 0
    assert s.rms_db < s.peak_db - 10                   # speech has crest


def test_gain_puts_the_sentence_peak_on_target():
    assert cal.gain_for(-29.5) == 19.5                 # Sam's Samson Q2U take
    assert cal.gain_for(-10.4) == 0.0                  # within 1 dB: leave it alone
    assert cal.gain_for(-3.0) == -7.0
    assert cal.gain_for(-70.0) == cal.MAX_GAIN_DB      # clamped
    assert cal.gain_for(-29.3) == 19.5                 # 0.5 dB steps


def test_sams_quiet_mic_is_detected_and_explained():
    c, adv = cal.calibrate("Microphone (Samson Q2U Microphone)", quiet(-29.5), voice(12.0, -29.5))
    codes = [a.code for a in adv]
    assert codes[0] == "quiet" and adv[0].level == "warn"
    assert "turn the mic up" in adv[0].text and "+19.5 dB" in adv[0].text
    assert c.gain_db == pytest.approx(19.5, abs=0.5)
    assert cal.ZONE[0] <= c.expected_peak_db <= cal.ZONE[1]


def test_clipping_mic_is_told_to_turn_down():
    x = np.clip(voice(12.0, 6.0), -1.0, 1.0)           # overdriven: flat tops at full scale
    c, adv = cal.calibrate("USB mic", quiet(0.0), x)
    assert adv[0].code == "clipping" and adv[0].level == "bad"
    assert "turn the mic level down" in adv[0].text.lower()
    assert c.gain_db == 0.0                            # never boost a clipped mic
    assert "good" not in [a.code for a in adv]


def test_a_little_loud_is_fine_and_turned_down():
    c, adv = cal.calibrate("USB mic", quiet(-3.0), voice(12.0, -3.0))
    assert adv[0].code == "loud" and adv[0].level == "info"
    assert c.gain_db == -7.0 and cal.ZONE[0] <= c.expected_peak_db <= cal.ZONE[1]


def test_far_too_quiet_asks_her_to_fix_it():
    c, adv = cal.calibrate("USB mic", quiet(-48.0), voice(12.0, -48.0))
    assert adv[0].code == "too_quiet" and adv[0].level == "bad"
    assert c.gain_db == cal.MAX_GAIN_DB


def test_good_level_says_so():
    c, adv = cal.calibrate("USB mic", quiet(-14.0), voice(12.0, -14.0))
    assert adv[0].code == "good" and adv[0].level == "ok"
    assert "+4 dB" in adv[0].text
    assert c.gain_db == 4.0


def test_noise_gate_in_the_chain_is_detected():
    gated = np.zeros(int(cal.QUIET_S * SR), np.float32)          # Krisp / Riverside-style gate
    c, adv = cal.calibrate("USB mic", gated, voice(12.0, -12.0))
    assert c.digital_silence and "gated" in [a.code for a in adv]
    assert "Krisp" in next(a.text for a in adv if a.code == "gated")


def test_noisy_room():
    loud_room = room_tone(cal.QUIET_S, -38.0, seed=3)
    c, adv = cal.calibrate("USB mic", loud_room, voice(12.0, -10.0) + room_tone(12.0, -38.0, seed=4))
    assert "noisy" in [a.code for a in adv]
    assert c.floor_db == pytest.approx(-38.0, abs=2.0)


def test_nothing_heard():
    _, adv = cal.calibrate("USB mic", room_tone(5.0, -60.0), room_tone(12.0, -60.0, seed=9))
    assert [a.code for a in adv] == ["no_speech"]
    _, adv = cal.calibrate("USB mic", np.zeros(SR * 5, np.float32), np.zeros(SR * 12, np.float32))
    assert [a.code for a in adv] == ["silent"]


def test_every_advice_is_plain_words():
    cases = [(quiet(p), voice(12.0, p)) for p in (-48.0, -29.5, -14.0, -3.0)]
    cases.append((np.zeros(SR * 5, np.float32), np.clip(voice(12.0, 6.0), -1, 1)))
    for q, r in cases:
        for a in cal.calibrate("m", q, r)[1]:
            assert a.title and a.level in ("ok", "info", "warn", "bad")
            assert "LUFS" not in a.text and "dBFS" not in a.text      # no jargon in her advice


# =========================================================================== setup checks

USB = "Microphone (Samson Q2U Microphone)"
ARRAY = "Microphone Array (Intel® Smart Sound Technology for Digital Microphones)"


@pytest.mark.parametrize("name,kind", [
    (USB, "usb"), ("Microphone (USB PnP Sound Device)", "usb"), (ARRAY, "laptop"),
    ("Microphone (Krisp Microphone)", "suppressor"), ("Microphone (NVIDIA Broadcast)", "suppressor"),
    ("CABLE Output (VB-Audio Virtual Cable)", "virtual"), ("Headset (Jabra Hands-Free AG Audio)", "bluetooth"),
    ("Microphone (Realtek(R) Audio)", "unknown"), ("Microphone (Steam Streaming Microphone)", "virtual"),
])
def test_classify_input(name, kind):
    assert cal.classify_input(name) == kind


def test_classify_output():
    assert cal.classify_output("Speakers (Realtek(R) Audio)") == "speakers"
    assert cal.classify_output("Headphones (Realtek(R) Audio)") == "headphones"
    # headphones in the Q2U's jack appear as "Speakers (<the mic>)"
    assert cal.classify_output("Speakers (Samson Q2U Microphone)", USB) == "headphones"
    assert cal.classify_output("LG ULTRAGEAR (NVIDIA High Definition Audio)") == "speakers"


def snap(**kw):
    base = dict(chosen=USB, inputs=[USB, ARRAY], default_input=USB, output="Headphones (Realtek(R) Audio)")
    base.update(kw)
    return cal.DeviceSnapshot(**base)


def test_setup_ok():
    adv = cal.setup_advice(snap())
    assert adv[0].code == "setup_ok"


def test_wrong_default_laptop_mic_with_usb_plugged_in():
    adv = cal.setup_advice(snap(chosen=ARRAY, using_default=True, default_input=ARRAY))
    assert adv[0].code == "wrong_mic" and adv[0].level == "bad"
    assert "default" in adv[0].title and USB in adv[0].text


def test_no_microphone_at_all():
    adv = cal.setup_advice(snap(chosen="Default microphone", inputs=[], found=False))
    assert [a.code for a in adv] == ["no_device"] and "Plug in" in adv[0].text


def test_laptop_mic_only():
    adv = cal.setup_advice(snap(chosen=ARRAY, inputs=[ARRAY]))
    assert adv[0].code == "laptop" and adv[0].level == "warn"


def test_krisp_and_rate_and_speakers():
    k = "Microphone (Krisp Microphone)"
    assert cal.setup_advice(snap(chosen=k, inputs=[USB, k]))[0].code == "suppressor"
    codes = [a.code for a in cal.setup_advice(snap(inputs=[USB, k]))]
    assert "suppressor_installed" in codes and "setup_ok" in codes
    assert cal.setup_advice(snap(rate_ok=False))[0].code == "rate"
    assert "low_rate" in [a.code for a in cal.setup_advice(snap(default_rate=16000))]
    adv = cal.setup_advice(snap(output="Speakers (Realtek(R) Audio)"))
    assert adv[0].code == "speakers" and "headphones" in adv[0].text.lower()


# =========================================================================== storage

def test_store_is_per_device():
    st = cal.CalibrationStore(DictSettings())
    a, _ = cal.calibrate(USB, quiet(-29.5), voice(12.0, -29.5))
    b, _ = cal.calibrate(ARRAY, quiet(-14.0), voice(12.0, -14.0))
    st.put(a)
    st.put(b)
    assert st.gain_db(USB) == a.gain_db and st.gain_db(ARRAY) == b.gain_db != a.gain_db
    assert st.get(USB).floor_db == a.floor_db              # the room floor is known
    assert st.get("Some other mic") is None and st.gain_db("Some other mic") == 0.0
    # MME cuts names to 31 characters; the same mic through WASAPI has the full name
    assert st.get(USB[:31]).gain_db == a.gain_db
    st.forget(USB)
    assert st.get(USB) is None


def test_store_survives_a_restart_in_qsettings(tmp_path):
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QSettings
    path = str(tmp_path / "s.ini")
    a, _ = cal.calibrate(USB, quiet(-29.5), voice(12.0, -29.5))
    cal.CalibrationStore(QSettings(path, QSettings.Format.IniFormat)).put(a)
    again = cal.CalibrationStore(QSettings(path, QSettings.Format.IniFormat)).get(USB)
    assert again is not None and again.gain_db == a.gain_db and again.device == USB
    assert "Checked" in again.summary()


def test_corrupt_store_entry_is_ignored():
    s = DictSettings()
    s.setValue("calibration/" + cal.device_key(USB), "{not json")
    assert cal.CalibrationStore(s).get(USB) is None


# =========================================================================== recorder

def _record(tmp_path, x, **kw):
    session = Session(tmp_path / "take")
    r = Recorder(session, source=ArraySource(x), use_radar=False, guard_enabled=False, **kw)
    r.start()
    r.source.done.wait(30)
    res = r.stop()
    return r, session, res


def test_calibrated_take_peaks_in_the_target_zone(tmp_path):
    """Acceptance: Sam's quiet mic (−29.5 dBFS) → after calibration a normal-voice take peaks
    within −12…−6 dBFS, in raw.flac itself."""
    c, _ = cal.calibrate(USB, quiet(-29.5), voice(12.0, -29.5, seed=7))
    take = voice(90.0, -29.5, seed=21)
    take[SR * 40:SR * 41] *= 10 ** (2.5 / 20)            # one livelier moment, +2.5 dB
    r, session, _ = _record(tmp_path, take, input_gain_db=c.gain_db, floor_db=c.floor_db)
    raw, _ = sf.read(session.raw, dtype="float32")
    peak = cal.db(np.abs(raw).max())
    assert cal.ZONE[0] <= peak <= cal.ZONE[1], peak
    rep = json.loads((session.dir / "capture.json").read_text("utf-8"))
    assert rep["input_gain_db"] == c.gain_db and rep["peak_dbfs"] == pytest.approx(peak, abs=0.1)
    assert r.status.level_advice == "" and r.status.clipped_samples == 0


def test_uncalibrated_quiet_take_warns_live(tmp_path):
    r, _, _ = _record(tmp_path, voice(30.0, -29.5))
    assert r.status.level_advice == "quiet"


def test_gain_reduction_still_reports_a_clipping_mic(tmp_path):
    hot = np.clip(voice(10.0, 6.0), -1.0, 1.0)
    r, _, _ = _record(tmp_path, hot, input_gain_db=-6.0)
    assert r.status.clipped_samples > 0                  # the mic clipped before our gain


# =========================================================================== live watch

def _feed(w, x):
    rms, peak = cal.block_levels(x)
    for a, b in zip(rms, peak):
        state = w.feed(float(a), float(b))
    return state


def test_level_watch():
    assert _feed(cal.LevelWatch(), voice(30.0, -9.0)) == ""
    assert _feed(cal.LevelWatch(), voice(30.0, -30.0)) == "quiet"
    assert _feed(cal.LevelWatch(), voice(30.0, -1.0)) == "loud"
    assert _feed(cal.LevelWatch(), voice(3.0, -30.0)) == ""            # not enough voice yet
    w = cal.LevelWatch()
    _feed(w, voice(30.0, -30.0))
    assert _feed(w, voice(25.0, -10.0, seed=8)) == ""                   # she moved closer: clears
    assert w.message == ""
    assert cal.LevelWatch(floor_db=-60.0).floor_db == -60.0


# =========================================================================== dialog

@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from ui import theme
    a = QApplication.instance() or QApplication([])
    theme.load_fonts()
    a.setStyleSheet(theme.stylesheet())
    return a


def _run_dialog(app, x, store, snapshot=None):
    from ui.calibrate import CalibrateDialog
    d = CalibrateDialog(store, [("Default microphone", None), (USB, 3)], USB,
                        source_factory=lambda dev: ArraySource(x),
                        snapshot=snapshot or (lambda dev, label: snap(chosen=label)),
                        privacy=lambda: [], first_run=True)
    d.show()
    app.processEvents()
    SHOTS.mkdir(exist_ok=True)
    d.grab().save(str(SHOTS / "ui_calibrate_setup.png"))
    d.start()
    end = time.perf_counter() + 20
    while d.phase != "result" and time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    return d


def test_dialog_measures_advises_and_saves(app):
    store = cal.CalibrationStore(DictSettings())
    x = np.concatenate([quiet(-29.5), voice(cal.READ_S, -29.5)])
    d = _run_dialog(app, x, store)
    assert d.phase == "result" and d.stack.currentIndex() == 2
    assert [a.code for a in d.advice][0] == "quiet"
    assert "gated" not in [a.code for a in d.advice]
    assert d.btn_save.isEnabled()
    d.grab().save(str(SHOTS / "ui_calibrate_result.png"))
    d.save()
    assert d.result is not None and store.get(USB).gain_db == pytest.approx(19.5, abs=0.5)
    assert d.mic_label == USB


def test_dialog_will_not_save_a_clipping_mic(app):
    store = cal.CalibrationStore(DictSettings())
    x = np.concatenate([quiet(0.0), np.clip(voice(cal.READ_S, 6.0), -1, 1)])
    d = _run_dialog(app, x, store)
    assert d.advice[0].code == "clipping" and not d.btn_save.isEnabled()
    assert "try again" in d.result_title.text().lower()
    d.restart()
    assert d.stack.currentIndex() == 0
    d.reject()
    assert store.get(USB) is None


def test_dialog_setup_page_flags_the_laptop_mic(app):
    from ui.calibrate import CalibrateDialog
    d = CalibrateDialog(cal.CalibrationStore(DictSettings()), [("Default microphone", None), (USB, 3)],
                        "Default microphone",
                        snapshot=lambda dev, label: snap(chosen=ARRAY if dev is None else USB,
                                                         using_default=dev is None, default_input=ARRAY),
                        privacy=lambda: [])
    assert d.setup_items[0].code == "wrong_mic" and d.btn_start.isEnabled()
    d.mic.setCurrentIndex(1)
    assert d.setup_items[0].code == "setup_ok"
    d.reject()
    blocked = CalibrateDialog(cal.CalibrationStore(DictSettings()), [("Default microphone", None)], "",
                              snapshot=lambda dev, label: snap(), privacy=lambda: [
                                  cal.Advice("privacy_apps_off", "bad", "Windows is blocking apps", "…")])
    assert not blocked.btn_start.isEnabled()
    assert "setup_ok" not in [a.code for a in blocked.setup_items]
    blocked.reject()


def test_main_window_shows_live_level_warning(app):
    from types import SimpleNamespace
    from ui.main_window import MainWindow
    w = MainWindow()
    w._reset_mic_warning()
    s = SimpleNamespace(clipped_samples=0, overflows=0, dropped_blocks=0, peak_db=-30.0, level_advice="quiet")
    w._check_mic(s)
    assert w.mic_warning.text() == cal.LIVE_MESSAGES["quiet"] and not w.mic_warning.isHidden()
    w.close()
