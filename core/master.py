"""Master once (CLAUDE.md Module C, adjusted to the channel's measured sound, §0.5).

Sam's edited takes are barely processed: ≈ −21 LUFS integrated, peaks ≈ −4 dBFS, no EQ or
compression. So the default chain is deliberately light:

  1. optional gentle compression (off by default)
  2. gain to `target_lufs` (BS.1770 integrated; digital-silence pauses are gated out)
  3. peak limiter at `ceiling_db` (rarely engages at −21 LUFS)
  4. seam fades at the start/end of the take, so loop joins go silence → silence
  5. encode once → session.m4a

The same function masters intro audio (if a channel ever uses one), so both land at the same loudness.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyloudnorm
import soundfile as sf
from scipy.ndimage import maximum_filter1d

from . import ffmpeg_tools as ff
from .config import RenderConfig


@dataclass(frozen=True)
class MasterConfig:
    target_lufs: float = -21.0
    ceiling_db: float = -1.5           # sample-peak ceiling (margin for AAC overshoot)
    limiter_window_ms: float = 8.0     # attack/hold half-window
    limiter_release_ms: float = 80.0
    seam_fade_ms: float = 1500.0
    compress: bool = False             # channel style: off
    polish: bool = False               # Studio polish chain (core/polish.py)
    max_gain_db: float = 30.0          # safety: never boost a near-silent take by more than this

    @classmethod
    def from_render(cls, cfg: RenderConfig, **kw) -> "MasterConfig":
        return cls(target_lufs=cfg.target_lufs, seam_fade_ms=cfg.seam_fade_ms, polish=cfg.polish, **kw)


@dataclass(frozen=True)
class MasterReport:
    input_lufs: float
    output_lufs: float
    gain_db: float
    limited_db: float                  # deepest limiter gain reduction applied
    peak_db: float
    duration_s: float


# BS.1770-4 K-weighting at 48 kHz (pre-filter shelf + RLB high-pass)
_K48 = (
    (np.array([1.53512485958697, -2.69169618940638, 1.19839281085285]),
     np.array([1.0, -1.69065929318241, 0.73248077421585])),
    (np.array([1.0, -2.0, 1.0]), np.array([1.0, -1.99004745483398, 0.99007225036621])),
)


_DITHER = np.random.default_rng(0).standard_normal(1 << 20) * 1e-10


def integrated_lufs(x: np.ndarray, sr: int) -> float:
    """BS.1770-4 integrated loudness, vectorised (≈ 1 s for 30 min; pyloudnorm takes ~20 s).
    Matches pyloudnorm to < 0.05 LU (see tests)."""
    if len(x) < sr * 0.4:
        return float("-inf")
    if sr != 48000:
        return float(pyloudnorm.Meter(sr).integrated_loudness(x.astype(np.float64)))
    from scipy.signal import lfilter
    y = np.asarray(x, dtype=np.float64)
    # Digital-silence pauses make the IIR state decay into denormal floats, ~100x slower on x86
    # (measured: 20 s vs 0.7 s for 33 min). A −200 dB dither keeps it normal; it can't move
    # the result (the absolute gate is at −70 LUFS).
    y = y + np.resize(_DITHER, len(y))
    for b, a in _K48:
        y = lfilter(b, a, y)
    c = np.concatenate(([0.0], np.cumsum(y * y)))
    block, step = int(0.4 * sr), int(0.1 * sr)
    starts = np.arange(0, len(y) - block + 1, step)
    z = (c[starts + block] - c[starts]) / block
    with np.errstate(divide="ignore"):
        lj = -0.691 + 10 * np.log10(z)
    z = z[lj > -70.0]
    if z.size == 0:
        return float("-inf")
    rel = -0.691 + 10 * np.log10(z.mean()) - 10.0
    z = z[-0.691 + 10 * np.log10(z) > rel]
    return float(-0.691 + 10 * np.log10(z.mean()))


def limit(x: np.ndarray, sr: int, ceiling_db: float, window_ms: float = 8.0,
          release_ms: float = 80.0) -> tuple[np.ndarray, float]:
    """Offline lookahead peak limiter. Only the regions around over-ceiling peaks are touched
    (at −21 LUFS that's a tiny fraction of a take), so it's fast and light on memory.

    Per region: required reduction → max-filter (gain is already down `window_ms` before the
    peak: lookahead) → moving average over the release time (smooth, and never below what a
    peak requires, because the max-filter spans the average)."""
    ceiling = 10 ** (ceiling_db / 20)
    overs = np.flatnonzero(np.abs(x) > ceiling)
    if overs.size == 0:
        return x, 0.0
    w = max(1, int(sr * window_ms / 1000))
    r = max(1, int(sr * release_ms / 1000))
    pad = 2 * (w + r)
    y = x.astype(np.float32, copy=True)
    deepest = 0.0
    # group overs into regions separated by more than 2·pad
    breaks = np.flatnonzero(np.diff(overs) > 2 * pad)
    for s_i, e_i in zip(np.concatenate(([0], breaks + 1)), np.concatenate((breaks, [overs.size - 1]))):
        lo, hi = max(0, overs[s_i] - pad), min(len(x), overs[e_i] + pad + 1)
        seg = np.abs(x[lo:hi]).astype(np.float64)
        req = np.where(seg > ceiling, 20 * np.log10(np.maximum(seg, 1e-12) / ceiling), 0.0)
        env = maximum_filter1d(req, size=2 * (w + r) + 1)
        c = np.concatenate(([0.0], np.cumsum(env)))
        half = r // 2
        idx = np.arange(len(seg))
        a, b = np.clip(idx - half, 0, len(seg)), np.clip(idx + half + 1, 0, len(seg))
        smooth = np.maximum((c[b] - c[a]) / (b - a), req)     # guarantee: never over the ceiling
        y[lo:hi] = x[lo:hi] * 10 ** (-smooth / 20)
        deepest = max(deepest, float(smooth.max()))
    return y, deepest


def seam_fades(x: np.ndarray, sr: int, fade_ms: float) -> np.ndarray:
    n = min(int(sr * fade_ms / 1000), len(x) // 2)
    if n <= 0:
        return x
    y = x.copy()
    ramp = (np.sin(np.linspace(0, np.pi / 2, n, dtype=np.float32))) ** 2
    y[:n] *= ramp
    y[-n:] *= ramp[::-1]
    return y


def gentle_compress(x: np.ndarray, sr: int, threshold_db: float = -16, ratio: float = 2.5,
                    attack_ms: float = 10, release_ms: float = 200) -> np.ndarray:
    """Optional (off for Sam's channel). RMS-envelope downward compressor, vectorised."""
    win = max(1, int(sr * (attack_ms + release_ms) / 2000))
    c = np.concatenate(([0.0], np.cumsum(x.astype(np.float64) ** 2)))
    idx = np.arange(len(x))
    lo, hi = np.clip(idx - win, 0, len(x)), np.clip(idx + win + 1, 0, len(x))
    level_db = 10 * np.log10((c[hi] - c[lo]) / (hi - lo) + 1e-12)
    over = np.maximum(0.0, level_db - threshold_db)
    return (x * 10 ** (-(over - over / ratio) / 20)).astype(np.float32)


def master_array(x: np.ndarray, sr: int, mc: MasterConfig = MasterConfig()) -> tuple[np.ndarray, MasterReport]:
    x = np.asarray(x, dtype=np.float32)
    in_lufs = integrated_lufs(x, sr)
    y = gentle_compress(x, sr) if mc.compress else x
    if mc.polish:
        from .polish import polish
        y = polish(y, sr)
    pre = integrated_lufs(y, sr) if (mc.compress or mc.polish) else in_lufs
    gain = 0.0 if not np.isfinite(pre) else float(np.clip(mc.target_lufs - pre, -mc.max_gain_db, mc.max_gain_db))
    y = (y * 10 ** (gain / 20)).astype(np.float32)
    y, limited = limit(y, sr, mc.ceiling_db, mc.limiter_window_ms, mc.limiter_release_ms)
    if limited > 0.05:   # limiting lowered loudness slightly: one corrective pass (cheap on 30 min)
        trim = mc.target_lufs - integrated_lufs(y, sr)
        if trim > 0.05:
            y, extra = limit((y * 10 ** (trim / 20)).astype(np.float32), sr, mc.ceiling_db,
                             mc.limiter_window_ms, mc.limiter_release_ms)
            gain += trim
            limited = max(limited, extra)
    y = seam_fades(y, sr, mc.seam_fade_ms)
    peak = float(20 * np.log10(np.abs(y).max(initial=1e-12)))
    # unlimited → loudness is exactly pre + gain (fades touch < 3 s); otherwise measure
    out_lufs = integrated_lufs(y, sr) if limited > 0.05 or not np.isfinite(pre) else pre + gain
    return y, MasterReport(in_lufs, out_lufs, gain, limited, peak, len(y) / sr)


def master_to_m4a(x: np.ndarray, sr: int, dst: Path, cfg: RenderConfig,
                  mc: MasterConfig | None = None) -> MasterReport:
    """Master an array and encode it ONCE to the session/intro format."""
    mc = mc or MasterConfig.from_render(cfg)
    y, rep = master_array(x, sr, mc)
    if sr != cfg.audio_sample_rate:
        dst = Path(dst)
        with tempfile.TemporaryDirectory(dir=dst.parent) as td:
            wav = Path(td) / "mastered.wav"
            sf.write(wav, y, sr, subtype="FLOAT")
            ff.encode_audio(wav, dst, cfg.audio_settings())
    else:
        ff.encode_pcm_parallel(y, sr, dst, cfg.audio_settings())   # split at silences, all cores
    return rep


def master_wav_file(src: Path, dst: Path, mc: MasterConfig = MasterConfig()) -> MasterReport:
    """(src_wav, dst_wav) form: plugs into AssetCache.intro(master=...)."""
    x, sr = sf.read(src, dtype="float32", always_2d=False)
    if x.ndim > 1:
        x = x.mean(axis=1)
    y, rep = master_array(x, sr, mc)
    sf.write(dst, y, sr, subtype="FLOAT")
    return rep
