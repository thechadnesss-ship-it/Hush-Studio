"""End-to-end session pipeline + the FINISH speed features (parallel encode, encode-free tail)."""
import numpy as np
import pytest

from core import ffmpeg_tools as ff
from core import renderer
from core.edl import EditList
from core.session import Session, denoise, finish, prepare
from synth import SR, plane, speech_take


def test_silence_split_points_land_in_silence():
    x, _ = speech_take(200.0, -24, seed=70)
    x[np.abs(x) < 1e-3] = 0.0                       # gate pauses to digital silence
    pts = ff.silence_split_points(x, SR, 4)
    assert len(pts) == 3 and all(x[p] == 0 for p in pts)
    assert all(abs(p - k * len(x) // 4) < 30 * SR for k, p in enumerate(pts, 1))


def test_parallel_encode_is_one_valid_session(tmp_path, cfg):
    x, _ = speech_take(240.0, -24, seed=71)
    x[np.abs(x) < 1e-3] = 0.0
    a, b = tmp_path / "one.m4a", tmp_path / "par.m4a"
    ff.encode_pcm_parallel(x, SR, a, cfg.audio_settings(), workers=1)
    ff.encode_pcm_parallel(x, SR, b, cfg.audio_settings(), workers=4)
    assert ff.audio_params(b) == ff.expected_audio_params(cfg.audio_settings())
    assert ff.duration(b) == pytest.approx(len(x) / SR, abs=0.2)     # + a few ms per join
    assert ff.duration(b) == pytest.approx(ff.duration(a), abs=0.2)


def test_encode_free_tail_ends_in_silence_at_exact_length(media, cfg, tmp_path):
    from core.master import master_to_m4a
    x, _ = speech_take(300.0, -24, seed=72)
    x[np.abs(x) < 1e-3] = 0.0
    session = tmp_path / "session.m4a"
    master_to_m4a(x, SR, session, cfg)
    c = cfg.with_(target_hours=1, fit_policy="exact")
    out = tmp_path / "out.mp4"
    plan, _ = renderer.render(c, session=session, black_loop=media["black"], out_path=out,
                              silence=media["cache"].silence())
    assert plan.tail_s > 0 and not (tmp_path / "tail.m4a").exists()    # nothing was encoded
    assert ff.duration(out) == pytest.approx(3600.0, abs=0.01)
    import subprocess
    r = subprocess.run([ff.ffmpeg_bin(), "-v", "error", "-sseof", "-1", "-i", str(out), "-vn", "-ac", "1",
                        "-f", "f32le", "-"], capture_output=True, check=True)
    assert np.abs(np.frombuffer(r.stdout, np.float32)).max() < 1e-3   # last second is silence
    r = ff.run([ff.ffmpeg_bin(), "-v", "error", "-xerror", "-i", str(out), "-f", "null", "-"], check=False)
    assert r.returncode == 0 and not r.stderr.strip(), r.stderr[-1000:]


def test_session_end_to_end_with_a_plane(tmp_path, cfg, media):
    """Import-style session: noisy take with a plane → edits (plane cut) → 1 h exact video."""
    x, _ = speech_take(240.0, -24, seed=73)
    y = (x + plane(240.0, t_peak=150.0, peak_db=-30)).astype(np.float32)
    s = Session(tmp_path / "sess")
    s.dir.mkdir()
    from core.denoise import SpectralGate
    clean, backend = denoise(s, y, denoiser=SpectralGate())   # synthetic voice: DeepFilterNet would remove it
    assert backend == "SpectralGate" and s.clean.exists()
    edl = prepare(s, y, clean)
    assert s.edits.exists()
    summ = edl.summary()
    assert summ["cuts"]["plane"]["count"] == 1, summ
    cut = next(c for c in edl.cuts if c.reason == "plane")
    assert cut.start < 140 < 160 < cut.end
    # Sam reviews: changes one pause by hand and saves (locked)
    loaded = EditList.load(s.edits)
    loaded.pauses[3].new_length, loaded.pauses[3].locked = 4.0, True
    loaded.save(s.edits)
    c = cfg.with_(target_hours=1)
    plan, out, rep, tm = finish(s, c, media["cache"], intro_card=None, clean=clean)
    assert ff.duration(out) == pytest.approx(3600.0, abs=0.01)
    assert rep.output_lufs == pytest.approx(c.target_lufs, abs=0.5)
    assert set(tm.steps) == {"apply_edits", "master_encode", "assets", "render"}


def test_raw_import_with_no_steps_renders_from_raw_flac(tmp_path, cfg, media):
    """T26: an imported take Sam renders without applying any step: the edit list names raw.flac and
    finish() reads that file (it used to assume clean.wav)."""
    import soundfile as sf
    from core.edl import RAW_SOURCE
    from core.session import import_take
    x, _ = speech_take(120.0, -24, seed=5)
    src = tmp_path / "take.wav"
    sf.write(src, x, SR, subtype="FLOAT")
    s = Session(tmp_path / "sess")
    raw = import_take(src, s)
    EditList(duration=len(raw) / SR, sample_rate=SR, source=RAW_SOURCE).save(s.edits)
    assert not s.clean.exists()
    c = cfg.with_(target_hours=1)
    plan, out, rep, _ = finish(s, c, media["cache"], intro_card=None)
    assert ff.duration(out) == pytest.approx(3600.0, abs=0.01)
    assert rep.output_lufs == pytest.approx(c.target_lufs, abs=0.5)
    assert not s.clean.exists()
