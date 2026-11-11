"""Studio polish: gentler dynamics, softer sibilance, same loudness, silences untouched, fast."""
import time
from pathlib import Path

import numpy as np
import pytest

from core.master import MasterConfig, integrated_lufs, master_array
from core.polish import polish
from synth import SR, colored_noise, speech_take

EDITED = Path(__file__).resolve().parent.parent / "9 - 10 nov edited.mp3"


def test_keeps_digital_silence_and_no_clipping():
    x, _ = speech_take(60.0, -20, seed=95)
    x[np.abs(x) < 1e-3] = 0.0
    y = polish(x)
    assert np.all(y[x == 0] == 0) and np.abs(y).max() < 1.0


def test_tames_a_harsh_s_and_evens_out_loud_words():
    rng = np.random.default_rng(3)
    x, _ = speech_take(30.0, -24, seed=96)
    s = colored_noise(SR // 4, rng, "white") * 0.2
    x[10 * SR: 10 * SR + len(s)] += s                          # a harsh "sss"
    x[20 * SR: 22 * SR] *= 4.0                                 # one loud phrase
    y = polish(x)
    from scipy.signal import butter, sosfilt
    band = butter(2, [5000, 9000], btype="bandpass", fs=SR, output="sos")
    seg = slice(10 * SR, 10 * SR + len(s))
    e = lambda v: np.mean(sosfilt(band, v)[seg] ** 2)
    assert 10 * np.log10(e(y) / e(x)) < -2.5                   # sibilance reduced
    loud, rest = slice(20 * SR, 22 * SR), slice(2 * SR, 8 * SR)
    ratio = lambda v: 10 * np.log10(np.mean(v[loud] ** 2) / np.mean(v[rest] ** 2))
    assert ratio(y) < ratio(x) - 2                             # loud phrase evened out


def test_master_with_polish_still_hits_channel_loudness():
    x, _ = speech_take(90.0, -30, seed=97)
    y, rep = master_array(x, SR, MasterConfig(polish=True))
    assert integrated_lufs(y, SR) == pytest.approx(-21.0, abs=0.5) and rep.peak_db <= -1.5


@pytest.mark.skipif(not EDITED.exists(), reason="Sam's edited take not present")
def test_fast_on_a_real_33_min_take():
    from core.session import load_mono_48k
    x = load_mono_48k(EDITED)
    # CPU time, not wall clock: other work on the machine (tasks, reviews) must not fail this.
    # Measured 2026-11-11 on the 4-core i5-6500: ~15 s CPU for 33 min of audio, so 25 s leaves
    # ~65% headroom while still catching a real slowdown. Polish is optional (off by default).
    t = time.process_time()
    polish(x)
    assert time.process_time() - t < 25
