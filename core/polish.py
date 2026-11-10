"""Studio polish: a gentle mastering chain that keeps Sam's natural style (no reverb, no effects).

  1. high-pass 70 Hz          rumble below her voice
  2. tone                     +1.5 dB warmth @ 180 Hz, −1 dB box @ 350 Hz, −1 dB softer air @ 10 kHz
  3. de-esser                 5–9 kHz reduced (max 6 dB) only where "s" sounds spike
  4. gentle compression       2:1 above her own speaking level −8 dB, at most 6 dB of reduction
Loudness is set afterwards by master.py (−21 LUFS), so the channel volume doesn't change.
Fully vectorised (lfilter); a −200 dB dither avoids denormal slowdowns in digital silence.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, lfilter, sosfilt

_DITHER = np.random.default_rng(1).standard_normal(1 << 20).astype(np.float64) * 1e-10


def _peaking(f0, gain_db, q, sr):
    a = 10 ** (gain_db / 40)
    w = 2 * np.pi * f0 / sr
    al = np.sin(w) / (2 * q)
    b = np.array([1 + al * a, -2 * np.cos(w), 1 - al * a])
    den = np.array([1 + al / a, -2 * np.cos(w), 1 - al / a])
    return b / den[0], den / den[0]


def _high_shelf(f0, gain_db, sr, s=0.7):
    a = 10 ** (gain_db / 40)
    w = 2 * np.pi * f0 / sr
    al = np.sin(w) / 2 * np.sqrt((a + 1 / a) * (1 / s - 1) + 2)
    c = np.cos(w)
    b = np.array([a * ((a + 1) + (a - 1) * c + 2 * np.sqrt(a) * al), -2 * a * ((a - 1) + (a + 1) * c),
                  a * ((a + 1) + (a - 1) * c - 2 * np.sqrt(a) * al)])
    den = np.array([(a + 1) - (a - 1) * c + 2 * np.sqrt(a) * al, 2 * ((a - 1) - (a + 1) * c),
                    (a + 1) - (a - 1) * c - 2 * np.sqrt(a) * al])
    return b / den[0], den / den[0]


def _env_db(power: np.ndarray, sr: int, ms: float, hop: int = 48) -> np.ndarray:
    """Smoothed level in dB, computed at 1 ms resolution (hop) and held per hop: envelopes move
    slowly, so this is ~50x cheaper than per-sample filtering with the same result."""
    n = len(power) // hop * hop
    blocks = power[:n].reshape(-1, hop).mean(axis=1)
    if n < len(power):
        blocks = np.append(blocks, power[n:].mean())
    a = np.exp(-1.0 / (sr / hop * ms / 1000))
    env = lfilter([1 - a], [1, -a], blocks)
    return np.repeat(10 * np.log10(env + 1e-12), hop)[: len(power)]


def polish(x: np.ndarray, sr: int = 48000) -> np.ndarray:
    y = np.asarray(x, dtype=np.float64) + np.resize(_DITHER, len(x))
    # 1 + 2: clean low end and tone, as one filter cascade
    sos = [butter(2, 70, btype="highpass", fs=sr, output="sos")]
    for b, a in (_peaking(180, 1.5, 0.8, sr), _peaking(350, -1.0, 1.0, sr), _high_shelf(10000, -1.0, sr)):
        sos.append(np.concatenate([b, a])[None, :])
    y = sosfilt(np.vstack(sos), y)
    # 3: de-esser: split off 5–9 kHz, turn it down only where it dominates
    sib = sosfilt(butter(2, [5000, 9000], btype="bandpass", fs=sr, output="sos"), y)
    excess_db = _env_db(sib * sib, sr, 5) - _env_db(y * y, sr, 5) + 9.0   # band within 9 dB of everything
    y = y - sib + sib * 10 ** (-np.clip(excess_db, 0, 6) / 20)
    # 4: gentle compression relative to her own speaking level
    env_db = _env_db(y * y, sr, 60)
    speech = env_db[env_db > -60]
    if speech.size:
        thr = np.percentile(speech[:: 48], 90) - 8.0
        y = y * 10 ** (-np.clip((env_db - thr) * 0.5, 0, 6.0) / 20)     # 2:1, max 6 dB
    out = y.astype(np.float32)
    out[np.asarray(x) == 0] = 0.0                            # keep her digital-silence pauses exact
    return out
