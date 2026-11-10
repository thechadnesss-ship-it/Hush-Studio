"""Mastering lands every take at the channel loudness, with peaks under the ceiling."""
from pathlib import Path

import numpy as np
import pytest

from core import ffmpeg_tools as ff
from core.master import MasterConfig, integrated_lufs, limit, master_array, master_to_m4a
from synth import SR, speech_take

EDITED_MP3 = Path(__file__).resolve().parent.parent / "9 - 10 nov edited.mp3"


def test_fast_meter_matches_pyloudnorm():
    import pyloudnorm
    for seed, lvl in [(40, -24), (41, -35)]:
        x, _ = speech_take(40.0, lvl, seed=seed)
        x = np.concatenate([x, np.zeros(SR * 20, np.float32)])
        ref = pyloudnorm.Meter(SR).integrated_loudness(x.astype(np.float64))
        assert integrated_lufs(x, SR) == pytest.approx(ref, abs=0.05)


def test_limiter_is_fast_on_a_long_take():
    import time
    x, _ = speech_take(240.0, -12, seed=42)           # loud: lots of overs
    t0 = time.perf_counter()
    y, red = limit(x, SR, -1.5)
    assert time.perf_counter() - t0 < 5
    assert np.abs(y).max() <= 10 ** (-1.5 / 20) + 1e-6 and red > 0


@pytest.mark.parametrize("level_db", [-40, -30, -24, -14])
def test_any_input_level_lands_on_target(level_db):
    x, _ = speech_take(90.0, level_db, seed=31)
    y, rep = master_array(x, SR, MasterConfig(target_lufs=-21.0))
    assert integrated_lufs(y, SR) == pytest.approx(-21.0, abs=0.5)
    assert rep.peak_db <= -1.5 + 1e-3


def test_limiter_holds_ceiling_and_is_smooth():
    t = np.arange(SR * 2) / SR
    x = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    x[SR: SR + 200] *= 4.0                      # a sharp 1.2 (+1.6 dBFS) burst
    y, red = limit(x, SR, ceiling_db=-1.5)
    assert np.abs(y).max() <= 10 ** (-1.5 / 20) + 1e-6
    assert red > 3
    # untouched far from the burst
    assert np.allclose(y[: SR // 2], x[: SR // 2]) and np.allclose(y[-SR // 2:], x[-SR // 2:])


def test_seam_fades_start_and_end_in_silence():
    x, _ = speech_take(30.0, -24, seed=32)
    x[:] += 0.05                                # worst case: DC at the edges
    y, _ = master_array(x, SR)
    assert abs(y[0]) < 1e-6 and abs(y[-1]) < 1e-6


def test_digital_silence_pauses_do_not_skew_loudness():
    """74% of Sam's edited take is digital silence; BS.1770 gating must ignore it."""
    x, _ = speech_take(60.0, -24, seed=33)
    padded = np.concatenate([x, np.zeros(SR * 170, dtype=np.float32)])
    assert integrated_lufs(padded, SR) == pytest.approx(integrated_lufs(x, SR), abs=0.3)


def test_encoded_session_keeps_loudness(tmp_path):
    from core.config import RenderConfig
    x, _ = speech_take(60.0, -35, seed=34)
    cfg = RenderConfig()
    rep = master_to_m4a(x, SR, tmp_path / "session.m4a", cfg)
    assert rep.output_lufs == pytest.approx(cfg.target_lufs, abs=0.5)
    r = ff.run([ff.ffmpeg_bin(), "-hide_banner", "-nostats", "-i", str(tmp_path / "session.m4a"),
                "-af", "ebur128", "-f", "null", "-"])
    line = [l for l in r.stderr.splitlines() if l.strip().startswith("I:")][-1]
    assert float(line.split()[1]) == pytest.approx(cfg.target_lufs, abs=0.5)


@pytest.mark.skipif(not EDITED_MP3.exists(), reason="Sam's edited reference take not present")
def test_sams_edited_take_needs_almost_no_change():
    """The target was measured from her edit: mastering it should barely touch it."""
    import subprocess
    r = subprocess.run([ff.ffmpeg_bin(), "-v", "error", "-i", str(EDITED_MP3), "-ac", "1", "-ar", "48000",
                        "-f", "f32le", "-"], capture_output=True, check=True)
    x = np.frombuffer(r.stdout, np.float32)
    import time
    t0 = time.perf_counter()
    _, rep = master_array(x, SR)
    elapsed = time.perf_counter() - t0
    assert abs(rep.gain_db) < 1.0, rep
    assert rep.limited_db < 0.5, rep
    assert elapsed < 15, f"mastering 33 min took {elapsed:.1f}s (budget 15 s)"
