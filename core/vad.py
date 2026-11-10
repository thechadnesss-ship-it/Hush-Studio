"""Energy-based speech/pause detection.

Sam's recordings are quiet-room voice takes (Riverside already gates pauses to ≈ −95 dBFS),
so a well-tuned energy VAD with hysteresis is reliable and has zero dependencies. Used by
the plane splicer (cut at natural pauses) and the auto-editor (pause shaping).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

FRAME_S = 0.010


@dataclass(frozen=True)
class VadConfig:
    min_speech_s: float = 0.06     # shorter blips are ignored (clicks, breaths)
    min_pause_s: float = 0.30      # shorter gaps are part of the phrase
    hangover_s: float = 0.12       # keep speech "on" briefly after energy drops (word tails)
    rel_threshold_db: float = 30.0 # speech = within this many dB of the loud speech level ...
    floor_margin_db: float = 12.0  # ... and at least this far above the noise floor


def frame_db(x: np.ndarray, sr: int, frame_s: float = FRAME_S) -> np.ndarray:
    hop = int(round(sr * frame_s))
    n = len(x) // hop
    if n == 0:
        return np.zeros(0)
    f = x[: n * hop].reshape(n, hop).astype(np.float64)
    return 10.0 * np.log10((f * f).mean(axis=1) + 1e-12)


def threshold_db(db: np.ndarray, cfg: VadConfig) -> float:
    if db.size == 0:
        return 0.0
    floor = float(np.percentile(db, 10))
    loud = float(np.percentile(db, 95))
    return max(floor + cfg.floor_margin_db, loud - cfg.rel_threshold_db)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[(start, end)) index runs where mask is True."""
    if mask.size == 0:
        return []
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.where(d == 1)[0], np.where(d == -1)[0]))


def detect_speech(x: np.ndarray, sr: int, cfg: VadConfig = VadConfig(),
                  thr_db: float | None = None) -> list[tuple[float, float]]:
    """Speech regions [(start_s, end_s)], merged across short pauses."""
    db = frame_db(x, sr)
    thr = threshold_db(db, cfg) if thr_db is None else thr_db
    on = db > thr
    # hangover: extend each "on" frame forward
    hang = int(round(cfg.hangover_s / FRAME_S))
    if hang:
        idx = np.where(on)[0]
        ext = np.zeros_like(on)
        for s, e in _runs(on):
            ext[s: min(len(on), e + hang)] = True
        on = ext
    runs = [(s, e) for s, e in _runs(on)]
    # merge across short pauses
    min_pause = int(round(cfg.min_pause_s / FRAME_S))
    merged: list[list[int]] = []
    for s, e in runs:
        if merged and s - merged[-1][1] < min_pause:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    min_sp = int(round(cfg.min_speech_s / FRAME_S))
    return [(s * FRAME_S, e * FRAME_S) for s, e in merged if e - s >= min_sp]


def pauses_from_speech(speech: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    """Complement of speech within [0, duration]: includes leading/trailing silence."""
    out, t = [], 0.0
    for s, e in speech:
        if s > t:
            out.append((t, s))
        t = max(t, e)
    if duration > t:
        out.append((t, duration))
    return out


def detect_pauses(x: np.ndarray, sr: int, cfg: VadConfig = VadConfig()) -> list[tuple[float, float]]:
    return pauses_from_speech(detect_speech(x, sr, cfg), len(x) / sr)
