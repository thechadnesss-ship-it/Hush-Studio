"""Signal-quality checks for a take and its processed counterpart.

The checks intentionally operate on the short take, never on the repeated video.  A
reference (raw) signal lets us distinguish an intended digital-silence pause from a
dropout introduced by denoising or encoding.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import signal

from .diagnostics import CLIP, SR, flat_top_runs, zero_gaps


@dataclass
class QualityReport:
    clipped_samples: int
    peak_dbfs: float
    true_peak_dbfs: float
    distortion_db: float
    dropouts: list[float] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.warnings

    def as_dict(self) -> dict:
        return {"clipped_samples": self.clipped_samples, "peak_dbfs": self.peak_dbfs,
                "true_peak_dbfs": self.true_peak_dbfs, "distortion_db": self.distortion_db,
                "dropouts": self.dropouts, "warnings": self.warnings, "passed": self.passed}


def _db(value: float) -> float:
    return float(20 * np.log10(max(float(value), 1e-12)))


def true_peak(x: np.ndarray, sr: int = SR, oversample: int = 4,
              chunk_samples: int = 1_000_000) -> float:
    """Return an inter-sample peak without materialising an oversampled whole take."""
    x = np.asarray(x, dtype=np.float32)
    if not len(x):
        return -120.0
    if oversample <= 1 or len(x) <= 8:
        return _db(np.max(np.abs(x), initial=0.0))
    # A small overlap keeps the FIR reconstruction valid at chunk boundaries.  Only
    # the centre of each reconstructed chunk contributes to the measured peak.
    overlap = 64
    peak = 0.0
    for start in range(0, len(x), chunk_samples):
        end = min(len(x), start + chunk_samples)
        left, right = max(0, start - overlap), min(len(x), end + overlap)
        y = signal.resample_poly(x[left:right], oversample, 1)
        centre_start = (start - left) * oversample
        centre_end = centre_start + (end - start) * oversample
        peak = max(peak, float(np.max(np.abs(y[centre_start:centre_end]), initial=0.0)))
    return _db(peak)


def _active(x: np.ndarray, frame: int = 960) -> np.ndarray:
    n = len(x) // frame
    if n == 0:
        return np.ones(len(x), dtype=bool)
    rms = np.sqrt(np.mean(x[:n * frame].reshape(n, frame).astype(np.float64) ** 2, axis=1))
    floor = max(10 ** (-70 / 20), float(np.percentile(rms, 20)) * 3.0)
    active = np.repeat(rms > floor, frame)
    return np.pad(active, (0, len(x) - len(active)), constant_values=False)


def dropout_times(source: np.ndarray, processed: np.ndarray, sr: int = SR,
                  max_spots: int = 200) -> list[float]:
    """Find 5--60 ms processed gaps where the source still contains sound."""
    n = min(len(source), len(processed))
    if n == 0:
        return []
    src, out = np.asarray(source[:n]), np.asarray(processed[:n])
    # Use the source for context; exact zeros are the strongest evidence of a cut.
    source_active = _active(src)
    gaps = zero_gaps(out, sr)
    result = []
    for t in gaps:
        a, b = int(t * sr), min(n, int(t * sr) + int(.060 * sr))
        if source_active[a:b].any():
            result.append(t)
    # A single model/encoder hiccup can produce adjacent zero runs.  Present one
    # playable spot per event and keep the review dialog bounded on long takes.
    merged: list[float] = []
    for t in result:
        if not merged or t - merged[-1] > 0.5:
            merged.append(t)
    return merged[:max_spots]


def analyze(source: np.ndarray, processed: np.ndarray, sr: int = SR,
            clip_limit_db: float = -1.0, max_distortion_db: float = -10.0,
            warn_true_peak: bool = True) -> QualityReport:
    """Check clipping, peak headroom, processing distortion, and source-relative gaps."""
    source = np.asarray(source, dtype=np.float32).reshape(-1)
    processed = np.asarray(processed, dtype=np.float32).reshape(-1)
    # Isolated full-scale samples are quantisation/float conversion artefacts;
    # flat runs are the reliable evidence of audible clipping.
    clipped = int(flat_top_runs(source, min_len=8))
    peak = _db(np.max(np.abs(processed), initial=0.0))
    tp = true_peak(processed, sr)
    n = min(len(source), len(processed))
    ref, out = source[:n], processed[:n]
    active = _active(ref)
    if active.any() and np.any(ref[active]):
        # Remove gain difference before measuring residual distortion.
        gain = float(np.dot(ref[active], out[active]) / (np.dot(ref[active], ref[active]) + 1e-12))
        residual = out[active] - gain * ref[active]
        distortion = _db(np.sqrt(np.mean(residual.astype(np.float64) ** 2)) /
                          (np.sqrt(np.mean(ref[active].astype(np.float64) ** 2)) + 1e-12))
    else:
        distortion = -120.0
    dropouts = dropout_times(ref, out, sr)
    warnings = []
    if clipped:
        warnings.append(f"{clipped:,} clipped samples in the source")
    if warn_true_peak and tp > clip_limit_db:
        warnings.append(f"true peak is {tp:.1f} dBFS (needs {clip_limit_db:.1f} dB headroom)")
    if distortion > max_distortion_db:
        warnings.append(f"processing distortion is {distortion:.1f} dB")
    if dropouts:
        warnings.append(f"{len(dropouts)} dropout{'s' if len(dropouts) != 1 else ''} detected")
    return QualityReport(clipped, round(peak, 2),
                         round(tp, 2), round(distortion, 2), dropouts, warnings)
