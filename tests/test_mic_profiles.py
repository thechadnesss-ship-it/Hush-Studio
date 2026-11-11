"""T29: mic profiles + autodetect (Samson Q2U, Rode NT1, Fifine, Generic) on fake device lists."""
from __future__ import annotations

import logging
import sys
import types

import numpy as np
import pytest

from core import mic_profiles as mp
from core.mic_select import candidates, plan

HOSTAPIS = [{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}, {"name": "Windows WDM-KS"}]
MME, DS, WASAPI, WDMKS = range(4)


def dev(name, api, ins=1, rate=48000.0):
    return {"name": name, "hostapi": api, "max_input_channels": ins, "max_output_channels": 0 if ins else 2,
            "default_samplerate": rate}


def on_all_apis(endpoint: str, device: str) -> list[dict]:
    """How Windows lists one USB mic: MME truncates to 31 chars, WDM-KS uses the bare device name."""
    full = f"{endpoint} ({device})"
    return [dev(full[:31], MME, rate=44100.0), dev(full, DS, rate=44100.0), dev(full, WASAPI),
            dev(device, WDMKS)]


BASE = [dev("Microsoft Sound Mapper - Input", MME, 2), dev("Primary Sound Capture Driver", DS, 2),
        dev("Speakers (Realtek(R) Audio)", WASAPI, 0), dev("Stereo Mix (Realtek HD Audio Stereo input)", WDMKS, 2),
        dev("Microphone (Steam Streaming Microphone Wave)", WDMKS)]
LAPTOP = on_all_apis("Microphone Array", "Realtek(R) Audio")
Q2U = on_all_apis("Microphone", "Samson Q2U Microphone")
FIFINE = on_all_apis("Microphone", "FIFINE K678 Microphone")
FIFINE_PNP = on_all_apis("Microphone", "2- USB PnP Sound Device")
RODE_USB = on_all_apis("Microphone", "RØDE NT-USB")
RODE_NT1_USB = on_all_apis("Microphone", "RODE NT1")
RODE_VIA_INTERFACE = on_all_apis("Analogue 1 + 2", "Focusrite USB Audio")


def detect(*groups, **kw):
    devices = [d for g in groups for d in g]
    return mp.detect_mic(devices, HOSTAPIS, **kw)


# ------------------------------------------------------------------ one mic at a time

@pytest.mark.parametrize("group, expected", [
    (Q2U, "samson_q2u"), (FIFINE, "fifine"), (FIFINE_PNP, "fifine"),
    (RODE_USB, "rode_nt1"), (RODE_NT1_USB, "rode_nt1"), (RODE_VIA_INTERFACE, "rode_nt1"),
])
def test_each_mic_alone_is_detected(group, expected):
    assert detect(BASE, LAPTOP, group).id == expected
    assert detect(group).id == expected                       # also without the laptop's own mic


def test_no_mic_and_unknown_mic_are_generic():
    assert detect().is_generic                                 # nothing at all
    assert detect(BASE).is_generic                             # only mappers / outputs
    assert detect(BASE, LAPTOP).is_generic                     # laptop array only
    assert detect(on_all_apis("Microphone", "Blue Yeti")).is_generic


def test_sounddevice_failure_is_generic(monkeypatch):
    def boom():
        raise OSError("PortAudio not initialised")
    monkeypatch.setattr(mp, "_query", boom)
    d = mp.detect()
    assert d.profile.is_generic and d.source == "fallback"


def test_unknown_names_are_logged_once():
    records: list[logging.LogRecord] = []
    h = logging.Handler()
    h.emit = records.append
    mp.log.addHandler(h)
    old = mp.log.level
    mp.log.setLevel(logging.INFO)
    try:
        mp._logged_unmatched.discard("Shure MV7 Test Mic")
        for _ in range(3):
            detect(on_all_apis("Microphone", "Shure MV7 Test Mic"))
    finally:
        mp.log.removeHandler(h)
        mp.log.setLevel(old)
    hits = [r for r in records if "Shure MV7 Test Mic" in r.getMessage()]
    assert len(hits) == 1


# ------------------------------------------------------------------ two mics: documented priority + override

def test_two_mics_pick_by_priority():
    assert detect(Q2U, FIFINE).id == "samson_q2u"
    assert detect(FIFINE, Q2U).id == "samson_q2u"              # order of devices does not matter
    assert detect(RODE_USB, FIFINE).id == "rode_nt1"
    assert detect(Q2U, RODE_USB).id == "samson_q2u"
    assert detect(Q2U, RODE_USB, FIFINE).id == "samson_q2u"


def test_strong_name_beats_weak_generic_name():
    # A Fifine that names itself beats an interface that only *probably* has the NT1 on it.
    assert detect(FIFINE, RODE_VIA_INTERFACE).id == "fifine"
    # A real Q2U beats a generic 'USB PnP' chip.
    assert detect(FIFINE_PNP, Q2U).id == "samson_q2u"


def test_override_wins_and_unknown_override_falls_back():
    assert detect(Q2U, FIFINE, override="fifine").id == "fifine"
    assert detect(Q2U, FIFINE, override="Rode NT1").id == "rode_nt1"    # by display name, even if absent
    assert detect(Q2U, FIFINE, override="generic").is_generic
    assert detect(Q2U, FIFINE, override="auto").id == "samson_q2u"
    assert detect(Q2U, FIFINE, override="no-such-mic").id == "samson_q2u"
    d = mp.detect(Q2U + FIFINE, HOSTAPIS, override="fifine")
    assert d.source == "override" and d.device_name == "FIFINE K678 Microphone"


# ------------------------------------------------------------------ names, config

def test_input_names_dedupe_across_host_apis():
    names = mp.input_names(BASE + Q2U + FIFINE_PNP, HOSTAPIS)
    assert names == ["Samson Q2U Microphone", "USB PnP Sound Device"]


def test_clean_name():
    assert mp.clean_name("Microphone (2- Samson Q2U Microphone)") == "Samson Q2U Microphone"
    assert mp.clean_name("Microphone (Samson Q2U Microphon") == "Samson Q2U Microphon"
    assert mp.clean_name("Samson Q2U Microphone") == "Samson Q2U Microphone"


def test_config_file_matches_builtins():
    from_file = mp.load_profiles()
    assert [p.id for p in from_file] == ["samson_q2u", "rode_nt1", "fifine", "generic"]
    builtin = {p.id: p for p in mp.BUILTIN_PROFILES}
    for p in from_file:
        b = builtin[p.id]
        assert (p.priority, p.patterns, p.weak_patterns, p.exclusive, p.best_host_api) == \
               (b.priority, b.patterns, b.weak_patterns, b.exclusive, b.best_host_api), p.id


def test_missing_or_broken_config_uses_builtins(tmp_path):
    assert [p.id for p in mp.load_profiles(tmp_path / "nope.toml")] == ["samson_q2u", "rode_nt1", "fifine", "generic"]
    bad = tmp_path / "mics.toml"
    bad.write_text("[[mic]]\nid = 'x'\npatterns = ['(unclosed']\n", encoding="utf-8")
    assert [p.id for p in mp.load_profiles(bad)][0] == "samson_q2u"
    bad.write_text("this is = = not toml", encoding="utf-8")
    assert mp.load_profiles(bad)[-1].is_generic


def test_custom_config_always_has_generic(tmp_path):
    f = tmp_path / "mics.toml"
    f.write_text("[[mic]]\nid = 'yeti'\nname = 'Blue Yeti'\npriority = 1\npatterns = ['yeti']\n", encoding="utf-8")
    profiles = mp.load_profiles(f)
    assert [p.id for p in profiles] == ["yeti", "generic"]
    assert mp.detect_mic(on_all_apis("Microphone", "Blue Yeti"), HOSTAPIS, profiles=profiles).id == "yeti"


# ------------------------------------------------------------------ open order (mic_select)

def test_q2u_open_order_wasapi_exclusive_first():
    devices = BASE + LAPTOP + Q2U
    profile, cands = plan(devices, HOSTAPIS)
    assert profile.id == "samson_q2u"
    q2u_idx = {i for i, d in enumerate(devices) if "Q2U" in d["name"]}
    assert {c.device for c in cands} == q2u_idx                 # never the laptop mic
    assert [(HOSTAPIS[devices[c.device]["hostapi"]]["name"], c.exclusive) for c in cands] == [
        ("Windows WASAPI", True), ("Windows WASAPI", False), ("Windows WDM-KS", False),
        ("Windows DirectSound", False), ("MME", False)]


def test_two_mics_open_only_the_winner():
    devices = FIFINE + Q2U
    cands = candidates(devices, HOSTAPIS)
    assert cands and all("Q2U" in devices[c.device]["name"] for c in cands)
    cands = candidates(devices, HOSTAPIS, profile="fifine")
    assert cands and all("FIFINE" in devices[c.device]["name"] for c in cands)


def test_generic_auto_and_absent_override_use_system_default():
    assert candidates(BASE + LAPTOP, HOSTAPIS) == []
    assert candidates([], HOSTAPIS) == []
    assert candidates(Q2U, HOSTAPIS, profile="rode_nt1") == []      # override for a mic that is not plugged in


def test_explicit_device_keeps_exclusive_and_gets_its_profile():
    devices = BASE + LAPTOP + Q2U
    wasapi_laptop = next(i for i, d in enumerate(devices) if d["hostapi"] == WASAPI and "Realtek" in d["name"]
                         and d["max_input_channels"])
    profile, cands = plan(devices, HOSTAPIS, wasapi_laptop)
    assert profile.is_generic
    assert cands[0] == type(cands[0])(wasapi_laptop, True, cands[0].label)
    profile, cands = plan(devices, HOSTAPIS, "Samson")
    assert profile.id == "samson_q2u" and cands[0].exclusive


# ------------------------------------------------------------------ MicSource with a fake sounddevice

class _FakeStream:
    opened: list = []

    def __init__(self, device=None, extra_settings=None, **kw):
        exclusive = getattr(extra_settings, "exclusive", False)
        _FakeStream.opened.append((device, exclusive))
        if exclusive:
            raise RuntimeError("exclusive mode refused")
        self.device = device if device is not None else 0
        self.samplerate = 48000.0

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


def _fake_sd(devices):
    sd = types.ModuleType("sounddevice")

    class WasapiSettings:
        def __init__(self, exclusive=False):
            self.exclusive = exclusive

    def query_devices(i=None, kind=None):
        return devices if i is None else devices[i]

    def query_hostapis(i=None):
        return HOSTAPIS if i is None else HOSTAPIS[i]

    sd.WasapiSettings, sd.InputStream = WasapiSettings, _FakeStream
    sd.query_devices, sd.query_hostapis = query_devices, query_hostapis
    return sd


def test_micsource_falls_back_from_exclusive_and_records_profile(monkeypatch):
    devices = BASE + LAPTOP + Q2U
    monkeypatch.setitem(sys.modules, "sounddevice", _fake_sd(devices))
    _FakeStream.opened = []
    from core.capture import MicSource
    src = MicSource()
    src.start(lambda b: None)
    assert _FakeStream.opened[0][1] is True and _FakeStream.opened[1][1] is False
    assert "Q2U" in src.info["device"] and src.info["mode"] == "WASAPI shared"
    assert src.info["mic_profile"] == "samson_q2u"
    src.stop()


def test_micsource_unknown_mic_uses_system_default(monkeypatch):
    devices = [dev("Microphone (Blue Yeti)", WASAPI)]
    monkeypatch.setitem(sys.modules, "sounddevice", _fake_sd(devices))
    _FakeStream.opened = []
    from core.capture import MicSource
    src = MicSource()
    src.start(lambda b: None)
    assert _FakeStream.opened == [(None, False)]
    assert src.info["mode"] == "system default" and src.info["mic_profile"] == "generic"
    src.stop()


def test_no_mic_array_source_still_records():
    from core.capture import ArraySource
    got = []
    src = ArraySource(np.zeros(48000, np.float32))
    src.start(got.append)
    assert src.done.wait(5)
    src.stop()
    assert sum(len(b) for b in got) == 48000
