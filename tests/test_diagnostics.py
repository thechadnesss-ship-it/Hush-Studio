"""T7 stage report: finds clipping (even after it was turned down) and dropouts per stage."""
import json

import numpy as np
import soundfile as sf

from core.diagnostics import flat_top_runs, format_report, measure, stage_report, zero_gaps
from synth import SR, speech_take


def test_clipping_is_found_even_after_gain():
    x, _ = speech_take(20.0, -24, seed=90)
    hot = np.clip(x / np.abs(x).max() * 4, -1, 1).astype(np.float32)
    assert measure(hot)["clipped"] > 0 and flat_top_runs(hot) > 0
    quiet = hot * np.float32(0.25)                       # mastered down 12 dB
    m = measure(quiet)
    assert m["clipped"] == 0 and m["flat"] > 0


def test_clean_speech_has_no_flat_tops_or_gaps():
    x, _ = speech_take(20.0, -40, seed=91)              # quiet: coarse 16-bit steps
    q = (np.round(x * 32767) / 32767).astype(np.float32)
    m = measure(q)
    assert m["flat"] == 0 and m["gaps"] == 0


def test_dropouts_are_found_but_pauses_are_not():
    x, regions = speech_take(20.0, -24, seed=92)
    x = x.astype(np.float32)
    s, e = regions[1]
    mid = int((s + e) / 2 * SR)
    x[mid:mid + 1440] = 0                                # 30 ms hole inside a phrase
    x[int(e * SR):int(regions[2][0] * SR)] = 0           # a whole pause: digital silence, fine
    assert zero_gaps(x) == [round(mid / SR, 3)]


def test_stage_report_on_a_session(tmp_path):
    x, _ = speech_take(12.0, -24, seed=93)
    sf.write(tmp_path / "raw.flac", x, SR, format="FLAC", subtype="PCM_24")
    sf.write(tmp_path / "clean.wav", x, SR, subtype="FLOAT")
    (tmp_path / "capture.json").write_text(json.dumps({"device": "Test mic", "clipped_samples": 0}))
    rep = stage_report(tmp_path)
    assert set(rep["stages"]) == {"raw.flac", "clean.wav"} and rep["capture"]["device"] == "Test mic"
    txt = format_report(rep)
    assert "raw.flac" in txt and "device=Test mic" in txt
