"""Background noise removal: the built-in spectral gate (DeepFilterNet tested when installed)."""
import os
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from core.denoise import (
    PassThrough,
    SpectralGate,
    SpectralGateConfig,
    deepfilter_exe,
    deepfilter_input_gain_db,
    denoise_array,
)
from core.vad import detect_speech
from synth import SR, colored_noise, speech_take


def rms_db(x):
    return 10 * np.log10(np.mean(x.astype(np.float64) ** 2) + 1e-20)


@pytest.fixture(scope="module")
def noisy():
    clean, regions = speech_take(60.0, -24, seed=21)
    noise = colored_noise(len(clean), np.random.default_rng(4), "pink") * 10 ** (-50 / 20)   # fan / hiss
    return clean, clean + noise, noise, regions


def masks(regions, n):
    sp = np.zeros(n, bool)
    for s, e in regions:
        sp[int(s * SR):int(e * SR)] = True
    return sp


def test_passthrough_is_identity(noisy):
    _, y, _, _ = noisy
    assert np.array_equal(denoise_array(y, PassThrough()), y)


def test_gate_removes_steady_noise_and_keeps_voice(noisy):
    clean, y, noise, regions = noisy
    out = denoise_array(y, SpectralGate())
    sp = masks(regions, len(y))
    sp[: int(3 * SR)] = True                         # ignore the estimator's warm-up
    pause = ~sp
    reduction = rms_db(y[pause]) - rms_db(out[pause])
    assert reduction > 12, f"only {reduction:.1f} dB removed in pauses"
    voice_change = rms_db(out[sp]) - rms_db(y[sp])
    assert abs(voice_change) < 1.5, f"voice level changed {voice_change:+.2f} dB"


def test_attenuation_limit_is_respected(noisy):
    _, y, _, regions = noisy
    out = denoise_array(y, SpectralGate(SpectralGateConfig(attenuation_limit_db=6)))
    pause = ~masks(regions, len(y))
    pause[: int(3 * SR)] = False
    assert rms_db(y[pause]) - rms_db(out[pause]) < 7.5


def test_streaming_is_chunk_size_invariant_and_aligned(noisy):
    _, y, _, _ = noisy
    a = denoise_array(y[: 10 * SR], SpectralGate(), chunk=480)
    b = denoise_array(y[: 10 * SR], SpectralGate(), chunk=48000)
    assert len(a) == len(b) == 10 * SR
    assert np.allclose(a, b, atol=1e-6)
    # latency compensated: speech onsets line up with the input
    assert detect_speech(a, SR)[0][0] == pytest.approx(detect_speech(y[: 10 * SR], SR)[0][0], abs=0.02)


def test_realtime_speed(noisy):
    import time
    _, y, _, _ = noisy
    t0 = time.perf_counter()
    denoise_array(y, SpectralGate())
    assert time.perf_counter() - t0 < 60.0 / 10          # ≥ 10x real time


def test_deepfilter_gain_stage_is_relative_to_quiet_input():
    normal_peak = 10 ** (-9.5 / 20)
    quiet_peak = 10 ** (-29.5 / 20)
    assert deepfilter_input_gain_db(np.array([normal_peak], dtype=np.float32)) == pytest.approx(0.0)
    assert deepfilter_input_gain_db(np.array([quiet_peak], dtype=np.float32)) == pytest.approx(17.5, abs=0.1)
    assert deepfilter_input_gain_db(np.zeros(100, dtype=np.float32)) == 0.0


def test_deepfilter_array_stages_the_cli_input(monkeypatch, tmp_path):
    seen = []

    def fake_deepfilter(src, dst, attenuation_limit_db=100.0, post_filter=False):
        audio, sr = sf.read(src, dtype="float32")
        seen.append(float(np.abs(audio).max(initial=0.0)))
        sf.write(dst, audio, sr, subtype="FLOAT")
        return dst

    monkeypatch.setattr("core.denoise.deepfilter_file", fake_deepfilter)
    quiet = np.full(SR * 2, 10 ** (-29.5 / 20), dtype=np.float32)
    from core.denoise import deepfilter_array
    denoised = deepfilter_array(quiet, SR, workers=1, work_dir=tmp_path)
    assert seen and 10 ** (-13 / 20) < seen[0] < 10 ** (-11 / 20)
    assert len(denoised) == len(quiet)


def test_deepfilter_floor_stays_at_validated_level_for_normal_take(monkeypatch, tmp_path):
    seen = []

    def fake_deepfilter(src, dst, attenuation_limit_db=100.0, post_filter=False):
        audio, sr = sf.read(src, dtype="float32")
        seen.append(20 * np.log10(np.sqrt(np.mean(audio[SR // 2:] ** 2)) + 1e-20))
        sf.write(dst, audio, sr, subtype="FLOAT")
        return dst

    monkeypatch.setattr("core.denoise.deepfilter_file", fake_deepfilter)
    normal = np.zeros(SR * 2, dtype=np.float32)
    normal[0] = 10 ** (-4 / 20)
    from core.denoise import deepfilter_array
    deepfilter_array(normal, SR, workers=1, work_dir=tmp_path)
    assert seen == [pytest.approx(-65.0, abs=0.5)]


def test_deepfilter_frozen_stage_gain_caps_louder_segment_once(monkeypatch, tmp_path):
    """A frozen live-take gain must not attenuate a later, louder segment twice."""
    seen = []

    def fake_deepfilter(src, dst, attenuation_limit_db=100.0, post_filter=False):
        audio, sr = sf.read(src, dtype="float32")
        seen.append(float(np.abs(audio).max(initial=0.0)))
        sf.write(dst, audio, sr, subtype="FLOAT")
        return dst

    monkeypatch.setattr("core.denoise.deepfilter_file", fake_deepfilter)
    t = np.arange(SR * 2, dtype=np.float32) / SR
    louder = (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    from core.denoise import deepfilter_array
    denoised = deepfilter_array(louder, SR, workers=1, work_dir=tmp_path, stage_gain_db=24.0)

    assert seen == [pytest.approx(0.9, abs=0.01)]
    assert np.max(np.abs(denoised)) == pytest.approx(np.max(np.abs(louder)), abs=0.01)
    assert rms_db(denoised) == pytest.approx(rms_db(louder), abs=0.05)


def test_quiet_take_keeps_phrase_endings_and_has_no_dropouts():
    """A take 20 dB below normal must keep the same relative VAD/edit path."""
    from core.audio_quality import analyze
    from core.master import MasterConfig, master_array

    normal, truth = speech_take(60.0, -24, seed=122)
    quiet = (normal * np.float32(0.1)).astype(np.float32)
    normal_sp = detect_speech(normal, SR)
    quiet_sp = detect_speech(quiet, SR)
    clean = denoise_array(quiet, SpectralGate())
    clean_sp = detect_speech(clean, SR)

    assert len(normal_sp) == len(quiet_sp) == len(clean_sp) == len(truth)
    for normal_region, quiet_region, clean_region in zip(normal_sp, quiet_sp, clean_sp):
        assert quiet_region[0] == pytest.approx(normal_region[0], abs=0.02)
        assert clean_region[0] == pytest.approx(quiet_region[0], abs=0.02)
        assert clean_region[1] >= quiet_region[1] - 0.05

    report = analyze(quiet, clean, warn_true_peak=False)
    assert report.dropout_count == 0
    assert report.distortion_db < -10.0
    mastered, master_report = master_array(clean, SR, MasterConfig(target_lufs=-21.0))
    assert master_report.gain_db < 30.0
    assert master_report.output_lufs == pytest.approx(-21.0, abs=0.5)
    assert np.isfinite(mastered).all()


REAL_RAW = __import__("pathlib").Path(__file__).resolve().parent.parent / "riverside_edit_- 9-10 nov_25_feb.wav"

_quiet_candidates = [
    Path(os.environ["HUSH_T25_QUIET_RAW"]) if "HUSH_T25_QUIET_RAW" in os.environ else None,
    Path(__file__).resolve().parent.parent / "Takes" / "2026-11-11_135033" / "raw.flac",
    Path.home() / "pika_meditate" / "Takes" / "2026-11-11_135033" / "raw.flac",
    Path.home() / "pika_meditate" / "Takes" / "2026-11-11_154948" / "raw.flac",
]
REAL_QUIET_RAW = next((p for p in _quiet_candidates if p is not None and p.exists()), _quiet_candidates[1])


@pytest.mark.skipif(deepfilter_exe() is None or not REAL_QUIET_RAW.exists(),
                    reason="needs DeepFilterNet and a real quiet take")
def test_deepfilternet_keeps_a_real_quiet_take_clean(tmp_path):
    """The actual CLI must preserve a quiet voice without introducing dropouts."""
    from core.audio_quality import analyze
    from core.denoise import deepfilter_array
    from core.diagnostics import zero_gaps
    from core.session import load_mono_48k

    x = load_mono_48k(REAL_QUIET_RAW)
    assert 20 * np.log10(np.abs(x).max() + 1e-20) < -20.0
    y = deepfilter_array(x, SR, workers=1, work_dir=tmp_path)
    report = analyze(x, y, warn_true_peak=False)
    assert len(y) == len(x)
    assert zero_gaps(y) == []
    assert report.dropout_count == 0
    assert report.distortion_db < -10.0


@pytest.mark.skipif(deepfilter_exe() is None or not REAL_RAW.exists(),
                    reason="needs DeepFilterNet and Sam's raw take (a neural model can't be tested on synthetic voices)")
def test_deepfilternet_removes_a_plane_from_real_voice(tmp_path):
    from core.denoise import deepfilter_array
    from core.session import load_mono_48k
    from synth import plane
    voice = load_mono_48k(REAL_RAW)[600 * SR: 690 * SR]        # 90 s of Sam, gated pauses
    sp = masks(detect_speech(voice, SR), len(voice))
    y = voice + plane(90.0, 45.0, -30)
    out = deepfilter_array(y, SR, workers=2, work_dir=tmp_path)
    assert len(out) == len(y)                                   # padded/aligned to the input
    pause = ~sp
    pause[: 3 * SR] = False
    assert rms_db(y[pause]) - rms_db(out[pause]) > 25           # measured ≈ 33 dB (gate: ≈ 15)
    assert abs(rms_db(out[sp]) - rms_db(voice[sp])) < 1.0       # her voice level kept


# ---------------------------------------------------------------- T7: no dropouts, clipping or cold joins

def _sam(seconds: float, start_s: float = 120.0):
    from core.session import load_mono_48k
    return load_mono_48k(REAL_RAW)[int(start_s * SR): int((start_s + seconds) * SR)]


needs_dfn_and_sam = pytest.mark.skipif(deepfilter_exe() is None or not REAL_RAW.exists(),
                                       reason="needs DeepFilterNet and Sam's raw take")


@needs_dfn_and_sam
def test_deepfilternet_keeps_word_endings_before_gated_silence(tmp_path):
    """The CLI zeroes frames under ≈ −72 dBFS and the 30 ms before them: on her gated take that
    cut 88% of phrase endings and left 370 dropouts per 33 min (before T7)."""
    from core.denoise import deepfilter_array
    from core.diagnostics import zero_gaps
    x = _sam(60.0)
    y = deepfilter_array(x, SR, workers=2, work_dir=tmp_path)
    assert zero_gaps(y) == []
    H = 480
    lv = 10 * np.log10((x[: len(x) // H * H].reshape(-1, H).astype(np.float64) ** 2).mean(1) + 1e-20)
    ends = [i * H for i in range(6, len(lv) - 5)
            if (lv[i:i + 5] < -72).all() and lv[i - 1] >= -72 and lv[i - 4:i].max() > -50]
    assert len(ends) >= 5
    zeroed = sum(bool(np.all(y[e - 1440:e] == 0)) for e in ends)
    assert zeroed == 0, f"{zeroed}/{len(ends)} phrase endings blanked"


@needs_dfn_and_sam
def test_deepfilternet_mid_word_split_matches_a_whole_file_run(tmp_path):
    """Live REC denoises ~60 s segments; a split inside a word must not click or restart cold."""
    from core.denoise import deepfilter_array
    x = _sam(20.0)
    sp = detect_speech(x, SR)
    s, e = max(sp, key=lambda r: r[1] - r[0])
    split = int((s + e) / 2 * SR)
    whole = deepfilter_array(x, SR, workers=1, work_dir=tmp_path)
    a = deepfilter_array(x[:split], SR, workers=1, work_dir=tmp_path)
    b = deepfilter_array(x[split:], SR, workers=1, work_dir=tmp_path, context=x[:split])
    joined = np.concatenate([a, b])
    w = slice(split - 4800, split + 4800)                         # ±100 ms around the join
    assert rms_db(whole[w]) - rms_db(joined[w] - whole[w]) > 35   # measured ≥ 44 dB (≈ 22 dB before T7)


@needs_dfn_and_sam
def test_deepfilternet_input_above_full_scale_is_not_clipped(tmp_path):
    """WASAPI float input can exceed ±1.0; the CLI's 16-bit output used to hard-clip it."""
    from core.denoise import deepfilter_array
    from core.diagnostics import flat_top_runs
    x = _sam(20.0)
    x = x * (10 ** (3 / 20) / np.abs(x).max())                    # peak +3 dBFS
    y = deepfilter_array(x, SR, workers=1, work_dir=tmp_path)
    assert np.abs(y).max() > 1.2
    assert flat_top_runs(y) == 0
