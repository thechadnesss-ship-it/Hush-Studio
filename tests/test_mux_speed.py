"""The headline promise: 30 min take → upload-ready 8 h MP4 in < 10 s, exact duration, no encoding."""
import time

import pytest

from core import ffmpeg_tools as ff
from core import renderer
from core.renderer import CodecMismatchError

MUX_BUDGET_S = 10.0


def _streams(path):
    info = ff.probe(path)
    by_type = {s["codec_type"]: s for s in info["streams"]}
    return info, by_type["video"], by_type["audio"]


def _assert_upload_ready(path, cfg, expected_s, tol=0.05):
    info, v, a = _streams(path)
    assert v["codec_name"] == "h264"
    assert f'{v["width"]}x{v["height"]}' == cfg.resolution
    assert a["codec_name"] == "aac" and a["profile"] == "LC"
    assert int(a["sample_rate"]) == cfg.audio_sample_rate
    assert int(a["channels"]) == cfg.audio_channels
    assert float(info["format"]["duration"]) == pytest.approx(expected_s, abs=tol)
    assert float(a["duration"]) == pytest.approx(expected_s, abs=tol)
    assert float(v["duration"]) == pytest.approx(expected_s, abs=tol)


def test_8h_mux_under_budget(media, cfg, tmp_path):
    cfg = cfg.with_(fit_policy="nearest")  # pure stream-copy path, no tail encode
    plan = renderer.plan_from_files(cfg, media["session"], media["intro"], media["black"])
    assert plan.loops == 16
    out = tmp_path / "output.mp4"

    t0 = time.perf_counter()
    engine = renderer.mux(plan, cfg, intro=media["intro"], session=media["session"],
                          black_loop=media["black"], out_path=out)
    elapsed = time.perf_counter() - t0

    try:
        print(f"\n8 h mux ({engine}): {elapsed:.2f} s, {out.stat().st_size / 2**20:.0f} MB, {plan.summary()}")
        assert engine == "stitch"
        assert elapsed < MUX_BUDGET_S, f"8 h mux took {elapsed:.1f} s (budget {MUX_BUDGET_S} s)"
        assert plan.total_s >= 8 * 3600 - plan.session_s / 2
        _assert_upload_ready(out, cfg, plan.total_s)
        assert not out.with_name(out.name + ".part").exists()
    finally:
        out.unlink(missing_ok=True)


def test_exact_policy_hits_target_to_the_second(media, cfg, tmp_path):
    c = cfg.with_(fit_policy="exact")
    out = tmp_path / "output.mp4"
    plan, _ = renderer.render(c, intro=media["intro"], session=media["session"],
                              black_loop=media["black"], out_path=out)
    try:
        assert plan.tail_s > 0
        _assert_upload_ready(out, c, 8 * 3600, tol=0.05)
    finally:
        out.unlink(missing_ok=True)


@pytest.mark.parametrize("hours", [3])
def test_output_decodes_without_errors(media, cfg, tmp_path, hours):
    """Full decode of a 3 h render: catches broken sample tables / offsets anywhere in the file."""
    c = cfg.with_(target_hours=hours)
    out = tmp_path / "output.mp4"
    plan, _ = renderer.render(c, intro=media["intro"], session=media["session"],
                              black_loop=media["black"], out_path=out)
    try:
        r = ff.run([ff.ffmpeg_bin(), "-v", "error", "-xerror", "-i", str(out), "-f", "null", "-"], check=False)
        assert r.returncode == 0 and not r.stderr.strip(), r.stderr[-2000:]
    finally:
        out.unlink(missing_ok=True)


def test_ffmpeg_fallback_path(media, cfg, tmp_path):
    """The FFmpeg concat fallback (used if the stitcher rejects an input) still produces a valid file."""
    c = cfg.with_(target_hours=3, fit_policy="nearest")
    plan = renderer.plan_from_files(c, media["session"], media["intro"], media["black"])
    files =[media["intro"]] + [media["session"]] * plan.loops
    out = tmp_path / "fallback.mp4"
    renderer._mux_ffmpeg(plan, files, media["black"], out, tmp_path, None, None)
    try:
        info, v, a = _streams(out)
        assert v["codec_name"] == "h264" and a["codec_name"] == "aac"
        # fragmented output rounds the 1 fps video track up to a whole frame
        assert float(a["duration"]) == pytest.approx(plan.total_s, abs=0.5)
        assert float(info["format"]["duration"]) == pytest.approx(plan.total_s, abs=1.0)
    finally:
        out.unlink(missing_ok=True)


def test_progress_reported(media, cfg, tmp_path):
    c = cfg.with_(target_hours=3)
    seen = []
    out = tmp_path / "o.mp4"
    renderer.render(c, intro=media["intro"], session=media["session"], black_loop=media["black"],
                    out_path=out, on_progress=seen.append)
    out.unlink()
    assert seen and seen[-1] == 1.0
    assert all(b >= a for a, b in zip(seen, seen[1:]))


def test_codec_mismatch_rejected(media, cfg, tmp_path):
    bad = tmp_path / "intro_44k.m4a"
    ff.encode_audio(media["intro_wav"], bad, {**cfg.audio_settings(), "sample_rate": 44100})
    cfg = cfg.with_(fit_policy="nearest")
    plan = renderer.plan_from_files(cfg, media["session"], bad, media["black"])
    with pytest.raises(CodecMismatchError):
        renderer.mux(plan, cfg, intro=bad, session=media["session"],
                     black_loop=media["black"], out_path=tmp_path / "x.mp4")


@pytest.fixture(scope="module")
def card(media, tmp_path_factory):
    """A 12 s synthetic intro card (moving test pattern, 25 fps, with audio to be dropped)."""
    src = tmp_path_factory.mktemp("card") / "card_src.mp4"
    ff.run([ff.ffmpeg_bin(), "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25:d=12",
            "-f", "lavfi", "-i", "sine=f=500:d=12",
            "-c:v", "libx264", "-bf", "3", "-c:a", "aac", "-shortest", str(src)])
    return src, media["cache"].intro_video(src)


def test_intro_card_and_black_share_one_h264_track(media, cfg, card, tmp_path):
    from core import mp4_stitch
    _, iv = card
    sig = lambda p: mp4_stitch.video_sig(mp4_stitch.read_track(p, b"vide").stsd)
    assert sig(iv) == sig(media["black"])                       # identical SPS/PPS
    info = ff.probe(iv)
    assert [s["codec_type"] for s in info["streams"]] == ["video"]   # source audio dropped
    assert float(info["format"]["duration"]) == pytest.approx(12.0, abs=0.05)


def test_render_with_card_no_intro_audio(media, cfg, card, tmp_path):
    """Sam's layout: silent card at the start of the video, her voice from t = 0, exact length."""
    _, iv = card
    c = cfg.with_(target_hours=1)
    out = tmp_path / "out.mp4"
    plan, _ = renderer.render(c, session=media["session"], black_loop=media["black"],
                              intro_video=iv, out_path=out)
    try:
        assert plan.video_head_s == pytest.approx(12.0, abs=0.05) and "card 0:12" in plan.summary()
        _assert_upload_ready(out, c, 3600.0, tol=0.05)
        r = ff.run([ff.ffmpeg_bin(), "-v", "error", "-xerror", "-i", str(out), "-f", "null", "-"], check=False)
        assert r.returncode == 0 and not r.stderr.strip(), r.stderr[-1500:]
        # 12 s of card at 25→30 fps, then 1 s black frames: far fewer frames than 30 fps throughout
        v = next(s for s in ff.probe(out)["streams"] if s["codec_type"] == "video")
        assert int(v["nb_frames"]) < 12 * 30 + 3600
    finally:
        out.unlink(missing_ok=True)


def test_mismatched_card_falls_back_to_ffmpeg(media, cfg, card, tmp_path):
    """A card not built by AssetCache (different H.264 params) can't share the track: the
    stitcher refuses and the FFmpeg path is used instead of producing a broken file."""
    from core import mp4_stitch
    src, _ = card
    c = cfg.with_(target_hours=1, fit_policy="nearest")
    plan = renderer.plan_from_files(c, media["session"], None, media["black"], src)
    with pytest.raises(mp4_stitch.UnsupportedInput):
        mp4_stitch.stitch(tmp_path / "x.mp4", [media["session"]] * plan.loops, media["black"],
                          plan.total_s, video_head=[src])


def test_cache_is_reused_and_keyed_by_settings(media, cfg):
    cache = media["cache"]
    black, intro = media["black"], media["intro"]
    m1, m2 = black.stat().st_mtime_ns, intro.stat().st_mtime_ns
    assert cache.black_loop() == black and black.stat().st_mtime_ns == m1
    assert cache.intro(media["intro_wav"]) == intro and intro.stat().st_mtime_ns == m2

    other = type(cache)(cfg.with_(loop_fps=2), cache.dir)
    assert other.black_loop_path() != black
    other = type(cache)(cfg.with_(audio_bitrate="96k"), cache.dir)
    assert other.intro_path(media["intro_wav"], "none") != intro
