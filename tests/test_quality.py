"""Real-signal quality checks: clipping and dropouts must be visible to the user."""
import numpy as np

from core.audio_quality import analyze, dropout_times, true_peak
from synth import SR, speech_take


def test_clean_speech_passes_quality_check():
    x, _ = speech_take(12.0, -24, seed=401)
    report = analyze(x, x)
    assert report.passed
    assert report.clipped_samples == 0
    assert report.dropouts == []
    assert report.true_peak_dbfs < -1


def test_deliberately_clipped_input_fails():
    x, _ = speech_take(12.0, -12, seed=402)
    clipped = np.clip(x * 3.0, -1.0, 1.0).astype(np.float32)
    report = analyze(clipped, clipped)
    assert not report.passed
    assert report.clipped_samples > 0
    assert any("clipped" in warning for warning in report.warnings)


def test_gap_against_source_fails_but_pause_does_not():
    x, regions = speech_take(12.0, -24, seed=403)
    gap = x.copy()
    start = int((regions[0][0] + 0.4) * SR)
    gap[start:start + int(.030 * SR)] = 0
    report = analyze(x, gap)
    assert not report.passed
    assert report.dropouts == [round(start / SR, 3)]
    assert dropout_times(x, x) == []


def test_true_peak_catches_inter_sample_peak():
    # Samples are below full scale, but the reconstructed waveform overshoots.
    x = np.array([0.0, 0.96, -0.96, 0.96, 0.0], dtype=np.float32)
    assert true_peak(x) > -1
