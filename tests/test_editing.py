"""T12: the edit list behind the waveform editor: timeline, manual edits, undo/redo, automated
features on all or part of the take. The audio array is never modified."""
import time

import numpy as np
import pytest

from core.edl import Cut, EditList, Gain, Insert, Marker, Pause
from core.editing import (MANUAL_XFADE_S, Editor, clips_length, clips_norm, clips_slice, compute_auto_pauses,
                          compute_normalise, compute_plane_cuts)
from synth import SR, plane, speech_take


def ramp(dur: float) -> np.ndarray:
    """Every sample different and identifiable: value = source time in seconds / 100."""
    return (np.arange(int(dur * SR)) / SR / 100).astype(np.float32)


def editor(dur=20.0, **kw):
    x = ramp(dur)
    return Editor(EditList(duration=dur, sample_rate=SR, **kw), x), x


# ----------------------------------------------------------------- timeline == the old renderer

def _legacy_render(e: EditList, x):
    """The renderer as it was before T12 (cuts + pauses only); the new timeline must match it."""
    sr, ef = e.sample_rate, int(round(e.pause_edge_s * e.sample_rate))
    fo = np.cos(np.linspace(0, np.pi / 2, ef, dtype=np.float32)) ** 2

    def piece(a, b):
        ch, t = [], int(round(a * sr))
        for p in sorted((p for p in e.pauses if p.start >= a - 1e-9 and p.end <= b + 1e-9
                         and p.length > 2 * e.pause_edge_s), key=lambda p: p.start):
            ps, pe = int(round(p.start * sr)), int(round(p.end * sr))
            h = x[t:ps + ef].copy()
            h[-ef:] *= fo
            ch += [h, np.zeros(max(0, int(round(p.new_length * sr)) - 2 * ef), np.float32), x[pe - ef:pe] * fo[::-1]]
            t = pe
        ch.append(x[t:int(round(b * sr))])
        return np.concatenate(ch)

    out = None
    for a, b in e.kept_ranges():
        p = piece(a, b)
        if out is None:
            out = p
            continue
        xf = min(int(round(e.crossfade_s * sr)), len(out), len(p))
        t = np.linspace(0, np.pi / 2, xf, dtype=np.float32)
        out = np.concatenate([out[:-xf], out[-xf:] * np.cos(t) + p[:xf] * np.sin(t), p[xf:]])
    return out


def test_timeline_matches_previous_renderer():
    x, _ = speech_take(90.0, -24, seed=11)
    e = EditList(90.0, SR, cuts=[Cut(20.0, 23.5, "plane"), Cut(60.0, 61.0, "noise"), Cut(70, 75, "plane", keep=True)],
                 pauses=[Pause(5.0, 7.0, 10.0, kind="marker"), Pause(30.0, 31.0, 1.0), Pause(40.0, 44.0, 2.0)])
    y, z = e.render(x), _legacy_render(e, x)
    assert len(y) == len(z)
    assert np.abs(y - z).max() < 1e-6
    assert e.output_duration() == pytest.approx(len(y) / SR, abs=1 / SR)


def test_mapping_round_trip():
    ed, _ = editor(30.0, pauses=[Pause(5.0, 8.0, 12.0)], cuts=[Cut(15.0, 17.0, "plane")])
    tl = ed.tl
    for o in np.linspace(0.1, ed.duration - 0.1, 50):
        s = tl.out_to_src(o * SR)
        seg = tl.seg_at(o * SR)
        if seg.kind == "audio":
            assert tl.src_to_out(s)[0] == pytest.approx(o * SR, abs=1)
    _, gone = tl.src_to_out(16.0 * SR)
    assert gone


# ----------------------------------------------------------------- manual edits

def test_delete_removes_exactly_the_selection_and_undo_restores():
    ed, x = editor(20.0)
    x0 = x.copy()
    before = ed.edl.to_json()
    ed.delete(5.0, 7.0)
    y = ed.render()
    assert len(y) / SR == pytest.approx(18.0 - MANUAL_XFADE_S, abs=1 / SR)
    assert np.array_equal(y[:int(4.9 * SR)], x[:int(4.9 * SR)])
    assert y[int(5.5 * SR)] == pytest.approx((7.5 + MANUAL_XFADE_S) / 100, abs=1e-5)   # crossfade pulls later audio earlier
    c = ed.edl.cuts[0]
    assert (c.reason, c.start, c.end) == ("manual", 5.0, 7.0)
    assert np.array_equal(x, x0)                            # the audio itself is never touched
    assert ed.undo() == "Delete"
    assert ed.edl.to_json() == before and ed.duration == 20.0
    ed.redo()
    assert len(ed.edl.cuts) == 1


def test_cut_then_paste_moves_audio():
    ed, x = editor(20.0)
    ed.cut(2.0, 3.0)
    assert ed.clipboard == [[2.0, 3.0]]
    ed.paste(10.0)
    y = ed.render()
    assert len(y) / SR == pytest.approx(20.0 - MANUAL_XFADE_S, abs=2 / SR)
    i = ed.edl.inserts[0]
    o, _ = ed.tl.src_to_out(i.at * SR)
    start = int(o) - int(1.0 * SR)                          # the insert sits just before source `at`
    mid = y[start + int(0.5 * SR)]
    assert mid == pytest.approx(2.5 / 100, abs=1e-5)


def test_paste_replaces_selection_and_is_one_undo_step():
    ed, _ = editor(20.0)
    ed.copy(1.0, 2.0)
    ed.paste(0, replace=(5.0, 8.0))
    assert ed.duration == pytest.approx(18.0 - MANUAL_XFADE_S, abs=2 / SR)
    ed.undo()
    assert ed.edl.cuts == [] and ed.edl.inserts == []


def test_paste_into_an_insert_splits_it():
    ed, _ = editor(20.0)
    ed.copy(1.0, 2.0)
    ed.paste(10.0)
    ed.copy(4.0, 4.5)
    ed.paste(10.5)                                          # middle of the first paste
    assert len(ed.edl.inserts) == 1
    assert clips_norm(ed.edl.inserts[0].clips, SR) == [[1.0, 1.5], [4.0, 4.5], [1.5, 2.0]]


def test_silence_keeps_length_and_zeroes_the_selection():
    ed, x = editor(20.0)
    ed.silence(4.0, 6.0)
    y = ed.render()
    assert len(y) == len(x)
    assert np.all(y[int(4.05 * SR):int(5.95 * SR)] == 0)
    assert np.array_equal(y[:int(3.99 * SR)], x[:int(3.99 * SR)])
    p = ed.edl.pauses[0]
    assert (p.kind, p.locked, p.new_length) == ("silence", True, 2.0)


def test_silence_over_existing_pauses_merges_them():
    ed, _ = editor(30.0, pauses=[Pause(5.0, 7.0, 10.0, kind="marker"), Pause(9.0, 10.0, 1.0)])
    before = ed.duration
    o0, _ = ed.tl.src_to_out(4.0 * SR)
    o1, _ = ed.tl.src_to_out(9.5 * SR)
    ed.silence(o0 / SR, o1 / SR)
    assert len(ed.edl.pauses) == 1
    p = ed.edl.pauses[0]
    assert (p.start, p.end) == (4.0, 10.0)
    assert ed.duration == pytest.approx(before, abs=2 / SR)    # nothing moved


def test_insert_silence_in_audio_and_in_a_pause():
    ed, x = editor(20.0, pauses=[Pause(10.0, 12.0, 2.0)])
    ed.insert_silence(5.0, 3.0)
    assert ed.duration == pytest.approx(23.0, abs=2 / SR)
    y = ed.render()
    assert np.all(y[int(5.01 * SR):int(7.99 * SR)] == 0)
    o, _ = ed.tl.src_to_out(11.0 * SR)
    ed.insert_silence(o / SR, 4.0)                          # inside a pause: the pause gets longer
    assert ed.edl.pauses[0].new_length == 6.0 and ed.edl.pauses[0].locked
    assert len(ed.edl.inserts) == 1


def test_delete_whole_pause_and_part_of_one():
    ed, _ = editor(30.0, pauses=[Pause(5.0, 8.0, 10.0), Pause(15.0, 17.0, 2.0)])
    a, _ = ed.tl.src_to_out(5.0 * SR)
    b, _ = ed.tl.src_to_out(8.0 * SR)
    ed.delete(a / SR + 2.0, a / SR + 5.0)                   # 3 s out of the 10 s silence
    assert ed.edl.pauses[0].new_length == pytest.approx(7.0)
    assert ed.edl.cuts == []
    a, _ = ed.tl.src_to_out(15.0 * SR)
    b, _ = ed.tl.src_to_out(17.0 * SR)
    ed.delete(a / SR, b / SR)                               # the whole pause: cut from the source
    assert len(ed.edl.cuts) == 1 and ed.edl.cuts[0].start == pytest.approx(15.0, abs=1e-3)


def test_delete_part_and_all_of_an_insert():
    ed, _ = editor(20.0)
    ed.copy(1.0, 3.0)
    ed.paste(10.0)
    ed.delete(10.5, 11.0)
    assert ed.edl.inserts[0].clips == [[1.0, 1.5], [2.0, 3.0]]
    ed.delete(10.0, 11.5)
    assert ed.edl.inserts == [] and ed.edl.cuts == []
    assert ed.duration == pytest.approx(20.0, abs=1 / SR)


def test_fades():
    ed, x = editor(20.0)
    ed.fade(15.0, 20.0, "out")
    y = ed.render()
    assert abs(y[-10]) < 1e-4 and y[int(14.9 * SR)] == x[int(14.9 * SR)]
    ed.fade(0.0, 2.0, "in")
    y = ed.render()
    assert y[10] == pytest.approx(0, abs=1e-6) and y[int(2.1 * SR)] == x[int(2.1 * SR)]
    assert [g.fade for g in ed.edl.gains] == ["out", "in"]


def test_markers_follow_the_audio():
    ed, _ = editor(30.0)
    m = ed.add_marker(20.0)
    ed.delete(2.0, 5.0)
    (pos, mm), = ed.markers()
    assert mm.id == m.id and pos == pytest.approx(17.0 - MANUAL_XFADE_S, abs=1e-3)
    ed.rename_marker(m.id, "deepener")
    assert ed.edl.markers[0].label == "deepener"
    ed.delete(16.0, 18.0)                                   # markers inside deleted audio go with it
    assert ed.edl.markers == []
    ed.undo()
    assert len(ed.edl.markers) == 1


def test_flub_proposals_are_editable_regions():
    ed, _ = editor(30.0, cuts=[Cut(10.0, 12.0, "flub", keep=True, proposed=True), Cut(20.0, 21.0, "plane")])
    marks = {m.cut.reason: m for m in ed.cut_marks()}
    assert not marks["flub"].active and marks["flub"].b - marks["flub"].a == pytest.approx(2.0, abs=1e-3)
    assert marks["plane"].active
    cid = marks["flub"].cut.id
    ed.decide_cut(cid, "accept")
    assert ed.duration == pytest.approx(30 - 2 - 1 - 2 * ed.edl.crossfade_s, abs=1e-3)
    ed.undo()
    assert ed.edl.cuts[0].proposed
    ed.decide_cut(marks["plane"].cut.id, "restore")
    assert ed.edl.cuts[1].keep


def test_undo_redo_stack():
    ed, _ = editor(20.0)
    ed.delete(1, 2)
    ed.silence(5, 6)
    ed.add_marker(3)
    assert [ed.undo(), ed.undo()] == ["Add marker", "Silence"]
    assert ed.can_redo and ed.redo_label() == "Silence"
    ed.redo()
    ed.fade(0, 1, "in")                                     # a new edit clears redo
    assert not ed.can_redo and ed.undo_label() == "Fade in"


# ----------------------------------------------------------------- automated features

def test_normalise_part_matches_the_rest():
    x, _ = speech_take(60.0, -24, seed=8)
    x = x.copy()
    x[int(20 * SR):int(30 * SR)] *= 10 ** (-8 / 20)          # she leaned away from the mic
    ed = Editor(EditList(60.0, SR, silence_pauses=False), x)
    db = compute_normalise(ed.tl, ed.audio, 20.0, 30.0)
    assert 5.0 < db < 9.0
    ed.apply_normalise(db, 20.0, 30.0)
    g = ed.edl.gains[0]
    assert (g.start, g.end, g.reason) == (20.0, 30.0, "normalise")
    whole = compute_normalise(ed.tl, ed.audio)
    ed.apply_normalise(whole)
    ed.apply_normalise(compute_normalise(ed.tl, ed.audio))   # twice: one entry, adjusted
    assert sum(g.start == 0 and g.reason == "normalise" for g in ed.edl.gains) == 1
    from core.master import integrated_lufs
    assert integrated_lufs(ed.render(), SR) == pytest.approx(-21.0, abs=0.3)


def test_auto_pauses_on_part_only_touches_that_part():
    x, _ = speech_take(120.0, -24, seed=9)
    ed = Editor(EditList(120.0, SR), x)
    ed.edl.pauses = []
    ed.silence(100.0, 101.0)                                # Sam's own silence must survive
    new = compute_auto_pauses(ed.audio, ed.edl)
    n = ed.apply_auto_pauses(new, 30.0, 60.0)
    assert n > 0
    auto = [p for p in ed.edl.pauses if not p.locked]
    assert auto and all(30.0 <= p.start and p.end <= 60.0 for p in auto)
    ed.apply_auto_pauses(new, 0.0, 120.0)
    assert any(p.kind == "silence" for p in ed.edl.pauses)
    assert len([p for p in ed.edl.pauses if not p.locked]) > len(auto)
    ed.undo()
    ed.undo()
    assert all(p.locked for p in ed.edl.pauses)


def test_remove_plane_noise_on_part_and_all():
    x, _ = speech_take(180.0, -24, seed=5)
    raw = x + plane(180.0, t_peak=100.0, peak_db=-30)
    ed = Editor(EditList(180.0, SR), x)
    assert compute_plane_cuts(raw, x, ed.edl, 0.0, 60.0) == []          # nothing in the first minute
    new = compute_plane_cuts(raw, x, ed.edl, 60.0, 180.0)
    assert len(new) == 1 and new[0].start < 85 and new[0].end > 115
    assert ed.apply_plane_cuts(new, 60.0, 180.0) == 1
    assert ed.apply_plane_cuts(new, 60.0, 180.0) == 1                 # re-run replaces, never doubles
    assert len(ed.edl.cuts) == 1
    ed.decide_cut(ed.edl.cuts[0].id, "restore")                       # Sam keeps it: a re-run respects that
    assert ed.apply_plane_cuts(new, 0.0, 180.0) == 0


def test_bring_back_from_audacity_and_undo():
    ed, x = editor(20.0, pauses=[Pause(5.0, 6.0, 3.0)])
    ed.add_marker(10.0)
    edited = ed.render()
    ed.bring_back(edited * 0.5, "audacity_1.wav")
    assert ed.edl.source == "audacity_1.wav" and ed.edl.pauses == []
    assert ed.edl.duration == pytest.approx(22.0, abs=1 / SR)
    assert ed.edl.markers[0].t == pytest.approx(10.0, abs=1e-3)
    assert not ed.on_original
    ed.undo()
    assert ed.edl.source == "clean.wav" and ed.audio is x and len(ed.edl.pauses) == 1


# ----------------------------------------------------------------- persistence + speed

def test_json_round_trip_and_old_files_load():
    e = EditList(30.0, SR, cuts=[Cut(1, 2, xfade_s=0.01)], pauses=[Pause(5, 6, 2, kind="silence")],
                 inserts=[Insert(10.0, [[1.0, 2.0], [None, 0.5]])], gains=[Gain(3, 4, fade="in")],
                 markers=[Marker(7.0, "here")], source="audacity_1.wav")
    e2 = EditList.from_json(e.to_json())
    assert e2 == e
    old = '{"duration": 10.0, "sample_rate": 48000, "cuts": [{"start": 1, "end": 2, "reason": "plane", "keep": false,' \
          ' "note": "", "id": "a", "proposed": false}], "pauses": [], "crossfade_s": 0.3, "pause_edge_s": 0.03,' \
          ' "silence_pauses": true}'
    e3 = EditList.from_json(old)
    assert e3.cuts[0].xfade_s is None and e3.source == "clean.wav" and e3.inserts == []


def test_clip_helpers():
    c = [[1.0, 2.0], [None, 1.0]]
    assert clips_length(c, SR) == 2 * SR
    assert clips_slice(c, SR // 2, int(1.5 * SR), SR) == [[1.5, 2.0], [None, 0.5]]
    assert clips_norm([[1.0, 2.0], [2.0, 3.0], [None, 1], [None, 1], [5.0, 5.0]], SR) == [[1.0, 3.0], [None, 2]]


def test_edits_stay_fast_on_a_40_min_take():
    """Each edit (op + timeline rebuild) on a 40 min take with ~250 pauses and a dozen cuts."""
    dur = 40 * 60.0
    rng = np.random.default_rng(0)
    pauses, t = [], 2.0
    while t < dur - 20:
        pauses.append(Pause(t, t + 3.0, 10.0 if rng.random() < 0.3 else 3.0))
        t += 8 + rng.random() * 3
    ed = Editor(EditList(dur, SR, pauses=pauses, cuts=[Cut(100.0 * k + 6.0, 100.0 * k + 6.5, "plane") for k in range(1, 13)]),
                np.zeros(16, np.float32))
    t0 = time.perf_counter()
    for k in range(10):
        a = 60.0 + 200 * k
        ed.delete(a, a + 1.5)
        ed.silence(a + 20, a + 22)
        ed.insert_silence(a + 40, 1.0)
        ed.fade(a + 50, a + 51, "out")
    per_op = (time.perf_counter() - t0) / 40
    assert per_op < 0.15, f"{per_op * 1000:.0f} ms per edit"
    t0 = time.perf_counter()
    for _ in range(10):
        ed.undo()
    assert (time.perf_counter() - t0) / 10 < 0.15


def test_trim_keeps_only_the_selection():
    ed, x = editor(20.0)
    ed.trim(5.0, 8.0)
    y = ed.render()
    assert len(y) / SR == pytest.approx(3.0, abs=2 / SR)
    assert y[int(1.5 * SR)] == pytest.approx(6.5 / 100, abs=1e-5)
    assert ed.undo() == "Trim audio" and ed.duration == 20.0


def test_block_rms_for_the_waveform():
    ed, x = editor(10.0, pauses=[Pause(4.0, 5.0, 3.0)])
    B = 256
    n = len(x) // B * B
    r = x[:n].reshape(-1, B)
    lo, hi, ms = ed.tl.block_peaks(r.min(1), r.max(1), B, (r.astype(np.float64) ** 2).mean(1).astype(np.float32))
    assert len(ms) == len(lo) and ms.max() == pytest.approx(hi.max() ** 2, rel=0.02)
    mid = int(ed.tl.src_to_out(4.5 * SR)[0]) // B
    assert ms[mid] == 0 and hi[mid] == 0                    # the shaped pause is digital silence
