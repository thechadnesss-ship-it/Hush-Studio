"""Auto-editor: finds Sam's spoken pause markers and shapes pauses like her manual edit."""
from pathlib import Path

import numpy as np
import pytest

from core.auto_edit import AutoEditConfig, auto_edit, find_markers, shape_pauses
from core.config import load_render_config
from core.edl import EditList
from core.vad import detect_pauses, detect_speech
from synth import SR, phrase, room_tone

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "riverside_edit_- 9-10 nov_25_feb.wav"


def take_with_markers(n_markers=12, seed=60):
    """Phrases of varied speech, with the SAME short utterance inserted n times between them."""
    rng = np.random.default_rng(seed)
    marker = phrase(1.1, -22, np.random.default_rng(999))          # identical every time
    pieces, t, truth = [room_tone(1.0, seed=seed)], 1.0, []
    for k in range(n_markers * 3):
        d = rng.uniform(2.0, 5.0)
        pieces += [phrase(d, -22, rng), room_tone(rng.uniform(1.0, 2.5), seed=k)]
        t += d + len(pieces[-1]) / SR
        if k % 3 == 1:
            pieces += [marker * rng.uniform(0.8, 1.2), room_tone(rng.uniform(1.0, 2.0), seed=k + 500)]
            truth.append(t)
            t += 1.1 + len(pieces[-1]) / SR
    pieces.append(room_tone(20.0, seed=7))
    x = np.concatenate(pieces).astype(np.float32)
    return x, truth


def test_markers_found_without_a_template():
    x, truth = take_with_markers()
    speech = detect_speech(x, SR)
    idx, tmpl = find_markers(x, SR, speech)
    assert tmpl is not None
    found = sorted(speech[i][0] for i in idx)
    assert len(found) == len(truth)
    assert all(abs(f - t) < 0.2 for f, t in zip(found, truth))


def test_nothing_repeats_means_no_markers():
    """No utterance repeats near-identically → no markers. (Synthetic 'speech' is one voice model
    and far too uniform for a realistic false-positive test; see the real-take test below.)"""
    rng = np.random.default_rng(64)
    pieces = []
    for k in range(40):
        f = rng.uniform(300, 4000)
        t = np.arange(int(rng.uniform(0.7, 1.6) * SR)) / SR
        chirp = np.sin(2 * np.pi * (f + rng.uniform(-200, 200) * t) * t) * 10 ** (-22 / 20)
        pieces += [chirp.astype(np.float32), room_tone(1.5, seed=k)]
    x = np.concatenate([room_tone(1.0)] + pieces)
    idx, tmpl = find_markers(x, SR, detect_speech(x, SR))
    assert idx == [] and tmpl is None


def test_saved_template_reused():
    x, truth = take_with_markers(seed=62)
    sp = detect_speech(x, SR)
    _, tmpl = find_markers(x, SR, sp)
    y, truth2 = take_with_markers(n_markers=5, seed=63)            # fewer than cluster_min
    idx, _ = find_markers(y, SR, detect_speech(y, SR), template=tmpl)
    assert len(idx) == len(truth2)


def test_shaped_output_matches_sams_style():
    x, truth = take_with_markers()
    speech = detect_speech(x, SR)
    pauses, _ = auto_edit(x, SR, speech, len(x) / SR)
    kinds = [p.kind for p in pauses]
    assert kinds.count("marker") == len(truth) and kinds[0] == "lead" and kinds[-1] == "tail"
    out = EditList(duration=len(x) / SR, sample_rate=SR, pauses=pauses).render(x)
    gaps = [e - s for s, e in detect_pauses(out, SR)]
    # Markers that happen to fall in the induction window use the measured 5 s pause.
    assert sum(1 for g in gaps if 9.5 < g < 10.5) == 10
    assert sum(1 for g in gaps if 4.5 < g < 5.5) == 2
    assert detect_speech(out, SR)[0][0] < 0.2                         # lead trimmed
    assert gaps[-1] == pytest.approx(13.0, abs=0.2)                   # tail kept at 13 s
    # the marker utterance itself is gone: fewer phrases out than in
    assert len(detect_speech(out, SR)) == len(speech) - len(truth)


def test_long_pause_capped_and_locked_respected():
    speech = [(1.0, 3.0), (3.5, 5.0), (40.0, 42.0)]
    p = shape_pauses(speech, 50.0, AutoEditConfig())
    long = [q for q in p if q.start == 5.0][0]
    assert long.new_length == 15.0
    short = [q for q in p if q.start == 3.0][0]
    assert short.new_length == pytest.approx(0.5)                    # ordinary pause keeps its length


def test_induction_markers_use_shorter_pause_but_normal_markers_do_not():
    speech = [(200.0, 201.0), (204.0, 205.0), (400.0, 401.0),
              (404.0, 405.0), (800.0, 801.0), (804.0, 805.0),
              (806.0, 807.0)]
    pauses = shape_pauses(speech, 810.0, AutoEditConfig(), markers=[1, 3, 5])
    markers = [p for p in pauses if p.kind == "marker"]
    assert [p.new_length for p in markers] == [5.0, 5.0, 10.0]


def test_induction_is_per_marker_and_has_explicit_boundaries():
    speech = [(100.0, 101.0), (104.0, 105.0), (120.0, 121.0),
              (180.0, 181.0), (184.0, 185.0), (200.0, 201.0),
              (720.0, 721.0), (724.0, 725.0), (730.0, 731.0)]
    pauses = shape_pauses(speech, 731.0, AutoEditConfig(), markers=[1, 3, 7])
    markers = [p for p in pauses if p.kind == "marker"]
    assert [p.new_length for p in markers] == [10.0, 5.0, 10.0]


def test_marker_in_cut_is_not_shaped():
    speech = [(200.0, 201.0), (204.0, 205.0), (210.0, 211.0),
              (400.0, 401.0), (404.0, 405.0), (410.0, 411.0)]
    pauses = shape_pauses(speech, 420.0, AutoEditConfig(), cuts=[(203.0, 206.0)], markers=[1, 4])
    markers = [p for p in pauses if p.kind == "marker"]
    assert len(markers) == 1
    assert markers[0].start == 401.0


def test_none_induction_pause_keeps_normal_marker_length():
    cfg = AutoEditConfig(induction_marker_pause_s=None)
    speech = [(200.0, 201.0), (204.0, 205.0), (210.0, 211.0)]
    pauses = shape_pauses(speech, 220.0, cfg, markers=[1])
    assert [p.new_length for p in pauses if p.kind == "marker"] == [10.0]


def test_induction_boundaries_include_180_but_exclude_720_and_need_full_take():
    at_start = shape_pauses([(170.0, 171.0), (180.0, 181.0), (190.0, 191.0)],
                            800.0, AutoEditConfig(), markers=[1])
    assert [p.new_length for p in at_start if p.kind == "marker"] == [5.0]

    at_end = shape_pauses([(710.0, 711.0), (720.0, 721.0), (730.0, 731.0)],
                          800.0, AutoEditConfig(), markers=[1])
    assert [p.new_length for p in at_end if p.kind == "marker"] == [10.0]

    short_take = shape_pauses([(170.0, 171.0), (180.0, 181.0), (190.0, 191.0)],
                              719.0, AutoEditConfig(), markers=[1])
    assert [p.new_length for p in short_take if p.kind == "marker"] == [5.0]


def test_render_config_zero_disables_induction_pause_and_validates_bounds():
    from core.config import RenderConfig
    assert RenderConfig(induction_marker_pause_s=0).induction_marker_pause_s is None
    with pytest.raises(ValueError):
        RenderConfig(marker_pause_s=0)
    with pytest.raises(ValueError):
        RenderConfig(induction_marker_pause_s=-1)
    with pytest.raises(ValueError):
        RenderConfig(induction_min_start_s=721, induction_end_s=720)


def test_render_config_exposes_induction_marker_settings():
    cfg = load_render_config()
    auto = cfg.auto_edit_config()
    assert auto.marker_pause_s == cfg.marker_pause_s
    assert auto.induction_marker_pause_s == cfg.induction_marker_pause_s
    assert auto.induction_end_s == cfg.induction_end_s
    assert auto.induction_min_start_s == cfg.induction_min_start_s


def test_prepare_uses_caller_render_config(tmp_path, monkeypatch):
    from core.config import RenderConfig
    from core.session import Session, prepare
    speech = [(200.0, 201.0), (204.0, 205.0), (210.0, 211.0)]
    clean = np.zeros(220 * SR, dtype=np.float32)
    monkeypatch.setattr("core.session.detect_speech", lambda audio, sr: speech)
    seen = {}
    def fake_auto_edit(*args, **kwargs):
        seen["cfg"] = args[4]
        return [], None
    monkeypatch.setattr("core.session.auto_edit", fake_auto_edit)
    cfg = RenderConfig(induction_marker_pause_s=0)
    (tmp_path / "s").mkdir()
    edl = prepare(Session(tmp_path / "s"), clean, clean, ear_events=[], render_cfg=cfg)
    assert seen["cfg"].induction_marker_pause_s is None
    assert edl.duration == pytest.approx(220.0)


@pytest.mark.skipif(not RAW.exists(), reason="Sam's raw reference take not present")
def test_real_take_reproduces_her_edit():
    from core.session import load_mono_48k
    x = load_mono_48k(RAW)
    speech = detect_speech(x, SR)
    pauses, tmpl = auto_edit(x, SR, speech, len(x) / SR)
    markers = [p for p in pauses if p.kind == "marker"]
    assert 80 <= len(markers) <= 100, len(markers)                    # ≈ 90 markers in her edit
    induction = [p for p in markers if p.new_length == pytest.approx(5.0)]
    assert len(induction) == 11
    # spot checks: positions where her edit replaced the word with ~10 s of silence (raw time, s)
    for t in (817.4, 994.8, 1026.3, 1484.3, 1924.8):
        assert any(p.start <= t <= p.end for p in markers), t
    # and ordinary sentences she kept are not markers (both phrases matched into her edit)
    assert not any(p.start <= 13.1 <= p.end for p in markers)
    out = EditList(duration=len(x) / SR, sample_rate=SR, pauses=pauses).render(x)
    assert 0.65 < float((out == 0).mean()) < 0.80                     # her edit: 71% digital silence
