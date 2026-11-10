"""Auto-editor: finds Sam's spoken pause markers and shapes pauses like her manual edit."""
from pathlib import Path

import numpy as np
import pytest

from core.auto_edit import AutoEditConfig, auto_edit, find_markers, shape_pauses
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
    assert sum(1 for g in gaps if 9.5 < g < 10.5) == len(truth)   # each marker → ~10 s of silence
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


@pytest.mark.skipif(not RAW.exists(), reason="Sam's raw reference take not present")
def test_real_take_reproduces_her_edit():
    from core.session import load_mono_48k
    x = load_mono_48k(RAW)
    speech = detect_speech(x, SR)
    pauses, tmpl = auto_edit(x, SR, speech, len(x) / SR)
    markers = [p for p in pauses if p.kind == "marker"]
    assert 80 <= len(markers) <= 100, len(markers)                    # ≈ 90 markers in her edit
    # spot checks: positions where her edit replaced the word with ~10 s of silence (raw time, s)
    for t in (817.4, 994.8, 1026.3, 1484.3, 1924.8):
        assert any(p.start <= t <= p.end for p in markers), t
    # and ordinary sentences she kept are not markers (both phrases matched into her edit)
    assert not any(p.start <= 13.1 <= p.end for p in markers)
    out = EditList(duration=len(x) / SR, sample_rate=SR, pauses=pauses).render(x)
    assert 0.65 < float((out == 0).mean()) < 0.80                     # her edit: 71% digital silence
