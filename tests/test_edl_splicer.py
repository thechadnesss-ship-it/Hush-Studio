"""Edit list rendering, speech/pause detection and plane-cut planning."""
import numpy as np
import pytest

from core.edl import Cut, EditList, Pause
from core.plane_guard.ear import Ear, holds_from_events
from core.plane_guard.splicer import add_plane_cuts, plan_cut
from core.vad import detect_pauses, detect_speech
from synth import SR, plane, speech_take


# ----------------------------------------------------------------- VAD

def test_vad_matches_true_phrases():
    x, truth = speech_take(120.0, -24, seed=3)
    got = detect_speech(x, SR)
    assert len(got) == len(truth)
    for (gs, ge), (ts, te) in zip(got, truth):
        assert abs(gs - ts) < 0.1 and abs(ge - te) < 0.25


def test_pauses_are_complement():
    x, _ = speech_take(60.0, -24, seed=4)
    sp, pa = detect_speech(x, SR), detect_pauses(x, SR)
    assert pa[0][0] == 0.0 and abs(pa[-1][1] - 60.0) < 0.02
    total = sum(e - s for s, e in sp) + sum(e - s for s, e in pa)
    assert total == pytest.approx(60.0, abs=0.05)


# ----------------------------------------------------------------- EDL render

def tone(dur, f=220.0):
    t = np.arange(int(dur * SR)) / SR
    return (0.3 * np.sin(2 * np.pi * f * t)).astype(np.float32)


def test_no_edits_is_identity():
    x = tone(10)
    e = EditList(duration=10, sample_rate=SR, silence_pauses=False)
    assert np.array_equal(e.render(x), x)


def test_cut_length_and_no_click():
    x = tone(20)
    e = EditList(duration=20, sample_rate=SR, cuts=[Cut(5.0, 9.0, "plane")])
    y = e.render(x)
    assert len(y) / SR == pytest.approx(20 - 4 - e.crossfade_s, abs=1e-3)
    assert len(y) / SR == pytest.approx(e.output_duration(), abs=1e-3)
    # equal-power join of (phase-shifted) tones: no sample-to-sample jump beyond a sine's own slope
    assert np.abs(np.diff(y)).max() < 0.3 * 2 * np.pi * 220 / SR * 1.5


def test_keep_anyway_and_overlapping_cuts():
    x = tone(30)
    e = EditList(duration=30, sample_rate=SR,
                 cuts=[Cut(5, 10, "plane"), Cut(8, 12, "flub"), Cut(20, 25, "plane", keep=True)])
    assert e.active_cuts() == [(5, 12)]
    assert len(e.render(x)) / SR == pytest.approx(30 - 7 - e.crossfade_s, abs=1e-3)
    s = e.summary()
    assert s["cuts"]["plane"]["kept_anyway"] == 1 and s["cuts"]["plane"]["removed_s"] == 5.0


def test_pause_shaping_makes_digital_silence_of_new_length():
    x, truth = speech_take(40.0, -24, seed=6)
    sp = detect_speech(x, SR)
    pauses = [(a[1], b[0]) for a, b in zip(sp, sp[1:])]
    e = EditList(duration=40.0, sample_rate=SR,
                 pauses=[Pause(s, t, new_length=8.0) for s, t in pauses])
    y = e.render(x)
    assert len(y) / SR == pytest.approx(e.output_duration(), abs=0.01)
    # each shaped pause contains an (8 s − edges) run of exact zeros
    zero = (y == 0).astype(np.int8)
    d = np.diff(np.concatenate(([0], zero, [0])))
    runs = (np.where(d == -1)[0] - np.where(d == 1)[0]) / SR
    long_runs = runs[runs > 5]
    assert len(long_runs) == len(pauses)
    assert np.allclose(long_runs, 8.0 - 2 * e.pause_edge_s, atol=0.002)


def test_json_round_trip(tmp_path):
    e = EditList(duration=12.5, sample_rate=SR, cuts=[Cut(1, 2, "plane", note="A320")],
                 pauses=[Pause(3, 4, 6.0, locked=True)])
    p = tmp_path / "edits.json"
    e.save(p)
    f = EditList.load(p)
    assert f == e


# ----------------------------------------------------------------- splicer

SPEECH = [(0.0, 4.0), (6.0, 10.0), (12.0, 15.0), (30.0, 34.0), (36.0, 40.0)]


def test_cut_starts_at_last_pause_before_red_and_trims_gap():
    c = plan_cut(red_t=14.0, clear_t=24.0, speech=SPEECH, duration=40)
    assert c.start == 10.0                 # end of the last complete phrase before RED
    assert c.end == 30.0 - 2.5             # silence after CLEAR trimmed to max_gap_s


def test_red_during_pause_cuts_from_where_she_stopped():
    # she finished her sentence at 15.0 (radar AMBER) and waited; RED arrives in the pause
    c = plan_cut(red_t=22.0, clear_t=26.0, speech=SPEECH, duration=40)
    assert c.start == 15.0


def test_rising_onset_moves_cut_earlier():
    # the ear's "rising" came at 5.0 (a pause), RED only at 14.0 mid-phrase
    c = plan_cut(red_t=14.0, clear_t=24.0, speech=SPEECH, duration=40, onset_t=5.0)
    assert c.start == 4.0
    # a "rising" too long before RED is ignored
    c = plan_cut(red_t=14.0, clear_t=24.0, speech=SPEECH, duration=40, onset_t=-100.0)
    assert c.start == 10.0


def test_no_pause_found_falls_back():
    c = plan_cut(red_t=50.0, clear_t=60.0, speech=[(30.0, 70.0)], duration=80)
    assert c.start == 49.0


def test_never_cuts_into_phrase_after_clear():
    c = plan_cut(red_t=14.0, clear_t=31.0, speech=SPEECH, duration=40)
    assert c.end == 30.0


def test_end_to_end_plane_is_removed():
    """take + plane → ear → holds → cut → render: the plane's loud part is gone, speech kept."""
    x, _ = speech_take(180.0, -24, seed=5)
    p = plane(180.0, t_peak=100.0, peak_db=-30)
    y = x + p
    ear = Ear(SR)
    ear.feed(y)
    holds = holds_from_events(ear.events, 180.0)
    rising = [e.t for e in ear.events if e.kind == "rising"]
    edl = EditList(duration=180.0, sample_rate=SR)
    cuts = add_plane_cuts(edl, holds, detect_speech(y, SR), onsets=rising)
    assert len(cuts) == 1
    c = cuts[0]
    assert c.start < 85 and 115 < c.end     # covers the flyover well beyond its loud core
    out_plane = edl.render(p)               # the same edit applied to the plane alone
    # what's left of the plane sits ≥ 20 dB under her voice (−24 dBFS RMS) and decays away
    c2 = np.concatenate(([0.0], np.cumsum(out_plane.astype(np.float64) ** 2)))
    rms = np.sqrt((c2[SR:] - c2[:-SR]) / SR)          # 1 s moving RMS
    assert 20 * np.log10(rms.max()) < -44
