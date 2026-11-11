"""T30: per-mic open path (exclusive → shared → others, profile block size/latency) and the level check."""
from __future__ import annotations

import logging
import sys
import threading
import types

import numpy as np
import pytest

from core import mic_check as mc
from core import mic_profiles as mp

SR = 48000
HOSTAPIS = [{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}, {"name": "Windows WDM-KS"}]
MME, DS, WASAPI, WDMKS = range(4)


def dev(name, api, rate=48000.0):
    return {"name": name, "hostapi": api, "max_input_channels": 1, "max_output_channels": 0,
            "default_samplerate": rate}


def mic(device: str) -> list[dict]:
    full = f"Microphone ({device})"
    return [dev(full[:31], MME, 44100.0), dev(full, DS, 44100.0), dev(full, WASAPI), dev(device, WDMKS)]


MICS = {"samson_q2u": mic("Samson Q2U Microphone"), "rode_nt1": mic("RODE NT1"),
        "fifine": mic("FIFINE K678 Microphone")}


# ------------------------------------------------------------------ fake sounddevice

class _Status:
    def __init__(self, overflow=False):
        self.input_overflow = overflow


class FakeStream:
    opened: list = []
    refuse: set = set()            # labels that fail: "exclusive", "shared", "WDM-KS", ...
    feed: np.ndarray | None = None # what the "mic" sends once started
    overflow_every: int = 0

    def __init__(self, device=None, samplerate=None, blocksize=None, latency=None, extra_settings=None,
                 callback=None, **kw):
        excl = getattr(extra_settings, "exclusive", False)
        conv = getattr(extra_settings, "auto_convert", False)
        api = "default" if device is None else HOSTAPIS[_DEVICES[device]["hostapi"]]["name"]
        kind = "exclusive" if excl else ("shared" if api == "Windows WASAPI" else api)
        FakeStream.opened.append(dict(device=device, kind=kind, auto_convert=conv, blocksize=blocksize,
                                      latency=latency, samplerate=samplerate))
        if kind in FakeStream.refuse:
            raise RuntimeError(f"Error opening InputStream: Device unavailable [{kind}]")
        self.device = device if device is not None else 0
        self.samplerate = float(samplerate)
        self.latency = 0.2
        self.blocksize = blocksize
        self.cb = callback
        self._t = None
        self._stop = threading.Event()

    def start(self):
        if FakeStream.feed is None:
            return

        def run():
            x = FakeStream.feed
            for n, i in enumerate(range(0, len(x), self.blocksize)):
                if self._stop.is_set():
                    break
                b = x[i:i + self.blocksize]
                over = bool(FakeStream.overflow_every) and n % FakeStream.overflow_every == FakeStream.overflow_every - 1
                self.cb(b.reshape(-1, 1), len(b), None, _Status(over))
        self._t = threading.Thread(target=run, daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join()

    def close(self):
        pass


_DEVICES: list[dict] = []


def fake_sd(monkeypatch, devices, refuse=(), feed=None, overflow_every=0):
    global _DEVICES
    _DEVICES = devices
    FakeStream.opened, FakeStream.refuse, FakeStream.feed = [], set(refuse), feed
    FakeStream.overflow_every = overflow_every
    sd = types.ModuleType("sounddevice")

    class WasapiSettings:
        def __init__(self, exclusive=False, auto_convert=False):
            self.exclusive, self.auto_convert = exclusive, auto_convert

    sd.WasapiSettings, sd.InputStream = WasapiSettings, FakeStream
    sd.query_devices = lambda i=None, kind=None: devices if i is None else devices[i]
    sd.query_hostapis = lambda i=None: HOSTAPIS if i is None else HOSTAPIS[i]
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    return sd


# ------------------------------------------------------------------ open path per mic

@pytest.mark.parametrize("pid", list(MICS))
def test_each_mic_opens_on_its_preferred_path(monkeypatch, pid, caplog):
    fake_sd(monkeypatch, MICS[pid])
    from core.capture import MicSource
    caplog.set_level(logging.INFO, logger="hush.capture")
    src = MicSource()
    src.start(lambda b: None)
    first = FakeStream.opened[0]
    assert len(FakeStream.opened) == 1 and first["kind"] == "exclusive"
    assert (first["samplerate"], first["blocksize"], first["latency"]) == (48000, 4800, "high")
    assert src.info["mic_profile"] == pid and src.info["mode"].startswith("WASAPI exclusive")
    assert "fell_back_from" not in src.info and src.info["blocksize"] == 4800
    assert any("input opened" in r.message and "WASAPI exclusive" in r.message and pid in r.message
               for r in caplog.records)
    src.stop()
    assert any("input closed" in r.message and "0 input overflow" in r.message for r in caplog.records)


def test_exclusive_held_by_another_app_falls_back_to_shared_with_converter(monkeypatch, caplog):
    fake_sd(monkeypatch, MICS["samson_q2u"], refuse={"exclusive"})
    from core.capture import MicSource
    caplog.set_level(logging.INFO, logger="hush.capture")
    src = MicSource()
    src.start(lambda b: None)
    assert [o["kind"] for o in FakeStream.opened] == ["exclusive", "shared"]
    assert FakeStream.opened[1]["auto_convert"] is True              # Windows runs some mics at 44.1 kHz
    assert src.info["mode"] == "WASAPI shared" and src.info["fell_back_from"] == ["WASAPI exclusive (direct from the mic)"]
    assert any("fell back from" in r.message for r in caplog.records)
    src.stop()


def test_whole_wasapi_busy_falls_through_other_apis_then_default(monkeypatch):
    fake_sd(monkeypatch, MICS["fifine"], refuse={"exclusive", "shared", "Windows WDM-KS"})
    from core.capture import MicSource
    src = MicSource()
    src.start(lambda b: None)
    assert [o["kind"] for o in FakeStream.opened] == ["exclusive", "shared", "Windows WDM-KS", "Windows DirectSound"]
    assert src.info["mode"] == "Windows DirectSound" and not FakeStream.opened[-1]["auto_convert"]
    src.stop()

    fake_sd(monkeypatch, MICS["fifine"], refuse={"exclusive", "shared", "Windows WDM-KS", "Windows DirectSound", "MME"})
    src = MicSource()
    src.start(lambda b: None)
    assert FakeStream.opened[-1]["kind"] == "default" and src.info["mode"] == "system default"
    src.stop()


def test_nothing_opens_raises_the_last_error(monkeypatch):
    fake_sd(monkeypatch, MICS["rode_nt1"],
            refuse={"exclusive", "shared", "Windows WDM-KS", "Windows DirectSound", "MME", "default"})
    from core.capture import MicSource
    with pytest.raises(RuntimeError, match="unavailable"):
        MicSource().start(lambda b: None)


def test_profile_open_settings_from_config(tmp_path):
    cfg = tmp_path / "mics.toml"
    cfg.write_text("[[mic]]\nid = 'samson_q2u'\nname = 'Samson Q2U'\npatterns = ['q2u']\nblocksize = 2400\n"
                   "latency = 0.1\nauto_convert = false\n", encoding="utf-8")
    p = mp.load_profiles(cfg)[0]
    assert (p.blocksize, p.latency, p.auto_convert) == (2400, 0.1, False)
    assert "100" in p.quiet_tip                         # tips not in the file come from the built-ins
    cfg.write_text("[[mic]]\nid = 'x'\nblocksize = 7\n", encoding="utf-8")
    assert mp.load_profiles(cfg)[0].id == "samson_q2u"  # bad value = broken file = built-ins


def test_shipped_config_open_settings():
    for p in mp.load_profiles():
        assert p.blocksize == 4800 and p.latency == "high" and p.auto_convert
        assert p.quiet_tip and p.loud_tip and p.room_tip


# ------------------------------------------------------------------ level verdicts

def speech(peak_db: float, room_db: float = -62.0, seconds: float = 5.0, seed: int = 3) -> np.ndarray:
    """A voiced buzz in 0.6 s words with 0.4 s gaps, peaking at `peak_db` (can clip above 0), over room noise."""
    t = np.arange(int(seconds * SR)) / SR
    phase = 2 * np.pi * np.cumsum(170 + 15 * np.sin(2 * np.pi * 4 * t)) / SR
    v = sum(np.sin(k * phase) / k for k in range(1, 12))
    v = v / np.abs(v).max() * ((t % 1.0) < 0.6)
    room = np.random.default_rng(seed).normal(0, 10 ** (room_db / 20), t.size)
    return np.clip(v * 10 ** (peak_db / 20) + room, -1, 1).astype(np.float32)


@pytest.mark.parametrize("pid", ["samson_q2u", "rode_nt1", "fifine", "generic"])
@pytest.mark.parametrize("take, peak_db, want", [("quiet", -30.0, "quiet"), ("good", -10.0, "good"),
                                                 ("too loud", -1.0, "loud"), ("clipping", 4.0, "loud")])
def test_quiet_good_and_loud_take_per_mic(pid, take, peak_db, want):
    p = mp.get_profile(pid)
    r = mc.level_verdict(speech(peak_db), p)
    assert r.verdict == want, (take, r.summary())
    assert r.title == mc.LEVEL_TITLES[want] and r.advice and r.mic == p.name
    assert abs(r.peak_dbfs - min(peak_db, 0.0)) < 1.0     # room noise rides on the peak
    assert r.rms_dbfs < r.peak_dbfs and r.floor_dbfs < -55
    if want == "quiet":
        assert r.advice.startswith(p.quiet_tip)
    elif want == "loud":
        assert p.loud_tip in r.advice
        assert ("clipping" in r.advice) == (take == "clipping")
    else:
        assert r.ok and "Nothing to change" in r.advice


def test_q2u_quiet_says_windows_level_100():
    r = mc.level_verdict(speech(-29.5, room_db=-60.0), mp.get_profile("samson_q2u"))   # Sam's measured level
    assert r.verdict == "quiet" and "Windows level to 100" in r.advice and "Sound" in r.advice


def test_nt1_too_loud_mentions_gain_and_noisy_room_mentions_room():
    nt1 = mp.get_profile("rode_nt1")
    r = mc.level_verdict(speech(2.0), nt1)
    assert r.verdict == "loud" and r.clipped and "gain knob" in r.advice and "very sensitive" in r.advice
    r = mc.level_verdict(speech(-10.0, room_db=-30.0), nt1)
    assert r.verdict == "good" and r.noisy_room and r.ok        # level fine; the room is a separate tip
    assert "hears the whole room" in r.advice and "above the room" in r.advice


def test_fifine_quiet_mentions_level_and_gain_knob():
    r = mc.level_verdict(speech(-35.0), mp.get_profile("fifine"))
    assert r.verdict == "quiet" and "100" in r.advice and "gain knob" in r.advice


def test_silence_and_no_speech():
    r = mc.level_verdict(np.zeros(5 * SR, np.float32), mp.get_profile("samson_q2u"))
    assert r.verdict == "silent" and r.advice == mc.MESSAGES["silent"][1]
    r = mc.level_verdict(speech(-90.0), mp.get_profile("samson_q2u"))       # only room noise
    assert r.verdict == "no_speech" and "Samson Q2U" in r.advice


def test_lost_sound_is_reported_and_not_ok():
    r = mc.level_verdict(speech(-10.0), mp.get_profile("fifine"), overflows=2)
    assert r.verdict == "good" and not r.ok and "Some sound was lost" in r.advice


def test_advice_is_plain_language():
    """No jargon in what Sam reads (dB is fine: the calibration screen uses it too)."""
    for pid in ("samson_q2u", "rode_nt1", "fifine", "generic"):
        p = mp.get_profile(pid)
        for peak, room in ((-30, -62), (-10, -62), (3, -62), (-10, -28)):
            text = mc.level_verdict(speech(peak, room), p).advice.lower()
            for jargon in ("dbfs", "rms", "wasapi", "overflow", "lufs", "host api"):
                assert jargon not in text, (pid, peak, text)


# ------------------------------------------------------------------ check_level end to end

def test_check_level_with_injected_recording(caplog):
    caplog.set_level(logging.INFO, logger="hush.capture")
    info = {"device": "Microphone (Samson Q2U Microphone)", "mode": "WASAPI shared", "host_api": "Windows WASAPI"}
    r = mc.check_level(3.0, record=lambda s: (speech(-30.0, seconds=s), info, mp.get_profile("samson_q2u"), 0))
    assert r.verdict == "quiet" and r.path == "WASAPI shared" and r.device.endswith("Microphone)")
    assert any("level check" in m and "WASAPI shared" in m and "overflows 0" in m for m in caplog.messages)


@pytest.mark.parametrize("pid, peak_db, want", [("samson_q2u", -29.5, "quiet"), ("rode_nt1", -1.0, "loud"),
                                                ("fifine", -10.0, "good")])
def test_check_level_records_through_the_take_path(monkeypatch, pid, peak_db, want):
    fake_sd(monkeypatch, MICS[pid], feed=speech(peak_db, seconds=3.5))
    r = mc.check_level(3.0)
    assert r.verdict == want and r.profile_id == pid and r.path.startswith("WASAPI exclusive")
    assert r.overflows == 0 and FakeStream.opened[0]["kind"] == "exclusive"


def test_check_level_counts_overflows(monkeypatch):
    fake_sd(monkeypatch, MICS["fifine"], refuse={"exclusive"}, feed=speech(-10.0, seconds=3.5), overflow_every=10)
    r = mc.check_level(3.0)
    assert r.path == "WASAPI shared" and r.overflows >= 2 and not r.ok


def test_cli_prints_verdict(monkeypatch, capsys):
    fake_sd(monkeypatch, MICS["samson_q2u"], feed=speech(-10.0, seconds=2.5))
    monkeypatch.setattr("core.log_setup.setup_logging", lambda *a, **k: None)
    assert mc.main(["--seconds", "2"]) == 0
    out = capsys.readouterr().out
    assert "Samson Q2U via WASAPI exclusive" in out and "Your level is good" in out
