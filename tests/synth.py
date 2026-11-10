"""Synthetic test audio: voice-like phrases, room tone, and plane flyovers (deterministic)."""
from __future__ import annotations

import numpy as np
from scipy import signal

SR = 48000


def db_to_amp(db: float) -> float:
    return 10 ** (db / 20)


def colored_noise(n: int, rng: np.random.Generator, color: str = "pink") -> np.ndarray:
    w = rng.standard_normal(n)
    f = np.fft.rfftfreq(n, 1 / SR)
    f[0] = f[1]
    shape = {"white": 1.0, "pink": 1 / np.sqrt(f), "brown": 1 / f}[color]
    x = np.fft.irfft(np.fft.rfft(w) * shape, n)
    return (x / (np.sqrt((x ** 2).mean()) + 1e-12)).astype(np.float32)


def room_tone(dur: float, level_db: float = -70.0, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return colored_noise(int(dur * SR), rng, "pink") * db_to_amp(level_db)


def phrase(dur: float, level_db: float, rng: np.random.Generator, sustained: bool = False) -> np.ndarray:
    """Voice-like: harmonic buzz through a formant band, syllable-rate amplitude modulation.
    `sustained=True` = one long held vowel with no syllable dips (worst case for the ear)."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    f0 = 180 + 15 * np.sin(2 * np.pi * 0.3 * t + rng.uniform(0, 6))
    ph = 2 * np.pi * np.cumsum(f0) / SR
    buzz = sum(np.sin(k * ph) / k for k in range(1, 25))
    b, a = signal.butter(2, [300, 3400], btype="band", fs=SR)
    v = signal.lfilter(b, a, buzz)
    if sustained:
        syll = np.ones_like(t)
    else:
        syll = 0.5 * (1 - np.cos(2 * np.pi * rng.uniform(3.5, 5.0) * t)) ** 1.5   # dips to ~0 between syllables
    env = np.minimum(1, np.minimum(t, dur - t) / 0.05)
    v = v * syll * env
    return (v / (np.sqrt((v ** 2).mean()) + 1e-12) * db_to_amp(level_db)).astype(np.float32)


def speech_take(dur: float, level_db: float = -24.0, seed: int = 2,
                phrase_s=(2.0, 6.0), pause_s=(1.0, 5.0),
                sustained: bool = False) -> tuple[np.ndarray, list[tuple[float, float]]]:
    """Alternating phrases and pauses over room tone. Returns (audio, true speech regions)."""
    rng = np.random.default_rng(seed)
    x = room_tone(dur, seed=seed)
    t, regions = 1.0, []
    while True:
        d = rng.uniform(*phrase_s)
        if t + d > dur - 1.0:
            break
        i = int(t * SR)
        p = phrase(d, level_db, rng, sustained=sustained)
        x[i:i + len(p)] += p
        regions.append((t, t + d))
        t += d + rng.uniform(*pause_s)
    return x, regions


def plane(dur_total: float, t_peak: float, peak_db: float, rise_s: float = 25.0, fall_s: float = 25.0,
          seed: int = 3) -> np.ndarray:
    """Jet flyover: low-frequency broadband roar with a smooth rise and fall (no transponder needed)."""
    rng = np.random.default_rng(seed)
    n = int(dur_total * SR)
    roar = colored_noise(n, rng, "brown")
    b, a = signal.butter(2, [60, 1800], btype="band", fs=SR)
    roar = signal.lfilter(b, a, roar).astype(np.float32)
    roar /= np.sqrt((roar ** 2).mean()) + 1e-12
    t = np.arange(n) / SR
    # gaussian-ish envelope in dB: peak_db at t_peak, ~40 dB down at ±rise/fall
    width = np.where(t < t_peak, rise_s, fall_s)
    env_db = peak_db - 40 * ((t - t_peak) / width) ** 2
    return (roar * 10 ** (np.maximum(env_db, -140) / 20)).astype(np.float32)
