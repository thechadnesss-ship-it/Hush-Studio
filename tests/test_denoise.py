"""Background noise removal: the built-in spectral gate (DeepFilterNet tested when installed)."""
import numpy as np
import pytest

from core.denoise import PassThrough, SpectralGate, SpectralGateConfig, deepfilter_exe, denoise_array
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


REAL_RAW = __import__("pathlib").Path(__file__).resolve().parent.parent / "riverside_edit_- 9-10 nov_25_feb.wav"


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
