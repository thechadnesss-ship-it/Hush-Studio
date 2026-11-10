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


def test_single_full_scale_sample_is_not_called_clipping():
    x, _ = speech_take(2.0, -24, seed=404)
    x[len(x) // 2] = 1.0
    assert analyze(x, x).clipped_samples == 0


def test_realistic_denoise_noise_removal_does_not_fail():
    clean, _ = speech_take(8.0, -24, seed=405)
    noise = np.random.default_rng(405).normal(0, 10 ** (-32 / 20), len(clean)).astype(np.float32)
    report = analyze(clean + noise, clean)
    assert report.passed


def test_true_peak_is_not_reconstructed_for_advisory_check(monkeypatch):
    x, _ = speech_take(2.0, -24, seed=407)
    monkeypatch.setattr("core.audio_quality.true_peak", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()))
    report = analyze(x, x, warn_true_peak=False)
    assert report.passed


def test_dropout_spots_are_merged_and_capped():
    x, regions = speech_take(8.0, -18, seed=406)
    y = x.copy()
    start = int((regions[0][0] + 0.3) * SR)
    y[start:start + 300] = 0
    y[start + 400:start + 700] = 0
    assert dropout_times(x, y) == [round(start / SR, 3)]
