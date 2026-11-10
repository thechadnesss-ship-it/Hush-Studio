"""Loop joins are inaudible: silence → silence, no clicks, no level jump."""
import subprocess

import numpy as np
import pytest

from core import ffmpeg_tools as ff
from core import renderer
from core.master import master_to_m4a
from synth import SR, speech_take


@pytest.fixture(scope="module")
def looped(tmp_path_factory, cfg, media):
    d = tmp_path_factory.mktemp("seams")
    # worst case for a seam: speech right up to the edges of the take (no natural silence)
    x, _ = speech_take(62.0, -22, seed=51, phrase_s=(5, 9), pause_s=(0.3, 0.6))
    x[: SR // 2] += 0.01 * np.sin(2 * np.pi * 300 * np.arange(SR // 2) / SR).astype(np.float32)
    session = d / "session.m4a"
    rep = master_to_m4a(x, SR, session, cfg)
    c = cfg.with_(target_hours=3.2 / 60, fit_policy="nearest")        # ~3 loops
    out = d / "out.mp4"
    plan, _ = renderer.render(c, session=session, black_loop=media["black"], out_path=out)
    r = subprocess.run([ff.ffmpeg_bin(), "-v", "error", "-i", str(out), "-vn", "-ac", "1", "-ar", str(SR),
                        "-f", "f32le", "-"], capture_output=True, check=True)
    y = np.frombuffer(r.stdout, np.float32)
    return plan, y, rep


def joins(plan):
    # seconds where loop k+1 starts (first segment's priming is skipped, the others keep theirs)
    return [plan.intro_s + k * plan.session_s for k in range(1, plan.loops)]


def test_loops_present(looped):
    plan, y, _ = looped
    assert plan.loops == 3 and len(joins(plan)) == 2
    assert len(y) / SR == pytest.approx(plan.total_s, abs=0.05)


def test_joins_are_silent(looped, cfg):
    plan, y, _ = looped
    for j in joins(plan):
        i = int(j * SR)
        win = y[i - int(0.2 * SR): i + int(0.2 * SR)]             # ±200 ms around the join
        assert 20 * np.log10(np.abs(win).max() + 1e-12) < -45, f"join at {j:.2f}s not silent"


def test_no_clicks_at_joins(looped):
    """A click = a sample-to-sample jump far above the signal's own local behaviour."""
    plan, y, _ = looped
    d = np.abs(np.diff(y))
    for j in joins(plan):
        i = int(j * SR)
        local = d[i - SR: i + SR]
        assert local.max() < 1e-3, f"jump {local.max():.2e} near join {j:.2f}s"


def test_level_matches_across_loops(looped):
    plan, y, _ = looped
    s = int(plan.session_s * SR)
    a, b = y[int(5 * SR): s - int(5 * SR)], y[s + int(5 * SR): 2 * s - int(5 * SR)]
    la, lb = (10 * np.log10(np.mean(v.astype(np.float64) ** 2)) for v in (a, b))
    assert abs(la - lb) < 0.2
