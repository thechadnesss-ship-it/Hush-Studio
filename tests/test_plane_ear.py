"""The ear must catch planes and never cry wolf on Sam's voice."""
from pathlib import Path

import numpy as np
import pytest

from core.plane_guard.ear import Ear, holds_from_events
from synth import SR, plane, speech_take

REAL_RAW = Path(__file__).resolve().parent.parent / "riverside_edit_- 9-10 nov_25_feb.wav"


def run(x, chunk=4800):
    ear = Ear(SR)
    for i in range(0, len(x), chunk):           # 100 ms chunks, like the audio callback
        ear.feed(x[i:i + chunk])
    return ear


def kinds(ear):
    return [e.kind for e in ear.events]


@pytest.fixture(scope="module")
def take():
    return speech_take(180.0, -24, seed=5)[0]


@pytest.mark.parametrize("peak_db", [-40, -30, -20])
def test_plane_goes_red_then_clear(take, peak_db):
    ear = run(take + plane(180.0, t_peak=100.0, peak_db=peak_db))
    assert kinds(ear) == ["calibrated", "rising", "red", "clear"], kinds(ear)
    (red, clear), = holds_from_events(ear.events, 180.0)
    assert 70 < red < 100, red            # RED before the plane peaks overhead
    assert 105 < clear < 150, clear       # CLEAR once it has gone, not at end of file
    rising = next(e.t for e in ear.events if e.kind == "rising")
    assert rising < red                   # early warning comes first


def test_faint_plane_warns_but_does_not_hold(take):
    ear = run(take + plane(180.0, t_peak=100.0, peak_db=-50))
    assert "red" not in kinds(ear)
    assert "rising" in kinds(ear)


def test_speech_alone_never_triggers(take):
    assert kinds(run(take)) == ["calibrated"]


def test_sustained_vowels_never_trigger():
    """Slow, held hypnotic vowels have no syllable dips: the between-harmonics estimate must hold."""
    x, _ = speech_take(150.0, -20, seed=9, phrase_s=(6.0, 10.0), pause_s=(0.5, 1.5), sustained=True)
    assert kinds(run(x)) == ["calibrated"]


def test_gated_input_never_triggers():
    """Riverside-style export: pauses are digital silence, room tone only while the gate is open."""
    x, regions = speech_take(150.0, -24, seed=11)
    gated = np.zeros_like(x)
    for s, e in regions:
        a, b = int((s - 0.05) * SR), int((e + 0.05) * SR)
        gated[a:b] = x[a:b]
    ear = run(gated)
    assert kinds(ear) == ["calibrated"]
    assert ear.room_db > -85                # calibrated on the open-gate room, not on digital silence


def test_chunk_size_does_not_matter(take):
    y = take + plane(180.0, t_peak=100.0, peak_db=-30)
    a, b = run(y, chunk=480), run(y, chunk=48000)
    assert [(e.kind, e.t) for e in a.events] == [(e.kind, e.t) for e in b.events]


def test_raw_minus_clean_second_opinion(take):
    """With a denoiser, noise = raw − clean is also used. A perfect denoiser makes the plane obvious."""
    p = plane(180.0, t_peak=100.0, peak_db=-30)
    ear = Ear(SR)
    y = take + p
    for i in range(0, len(y), 4800):
        ear.feed(y[i:i + 4800], clean=take[i:i + 4800])
    assert "red" in kinds(ear)


def test_realtime_speed(take):
    import time
    t0 = time.perf_counter()
    run(take)
    assert time.perf_counter() - t0 < 180.0 / 20      # ≥ 20x real time


@pytest.mark.skipif(not REAL_RAW.exists(), reason="Sam's raw reference take not present")
def test_real_take_has_no_false_alarms():
    import soundfile as sf
    from scipy import signal
    x, sr = sf.read(REAL_RAW, dtype="float32")
    x = signal.resample_poly(x, 160, 147).astype(np.float32) if sr == 44100 else x
    ear = Ear(SR)
    ear.feed(x)
    assert "red" not in kinds(ear), [(e.kind, e.t) for e in ear.events]
