"""Live recording pipeline, driven by an ArraySource instead of a microphone."""
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from core.capture import BLOCK, ArraySource, Recorder
from core.session import Session
from synth import SR, plane, speech_take

REAL_RAW = Path(__file__).resolve().parent.parent / "riverside_edit_- 9-10 nov_25_feb.wav"


def record(x, tmp_path, realtime=False, pause_at=None, **kw):
    src = ArraySource(x, realtime=realtime)
    statuses = []
    rec = Recorder(Session(tmp_path / "s"), source=src, use_radar=False,
                   on_status=lambda s: statuses.append((s.take_s, s.light, s.paused)), **kw)
    rec.start()
    if pause_at is not None:
        a, b = pause_at
        while rec.take_s < a:
            pass
        rec.pause()
        src_t0 = len(statuses)
        while len(statuses) - src_t0 < int((b - a) * 10):
            pass
        rec.resume()
    src.done.wait()
    return rec, rec.stop(), statuses


def test_records_raw_flac_and_sees_the_plane(tmp_path):
    x, _ = speech_take(180.0, -24, seed=80)
    y = (x + plane(180.0, 100.0, -30)).astype(np.float32)
    rec, res, st = record(y, tmp_path)
    raw, sr = sf.read(res.raw_path, dtype="float32")
    assert sr == SR and len(raw) == len(y) and np.allclose(raw, y, atol=2e-6)   # 24-bit FLAC
    assert res.duration_s == pytest.approx(180.0, abs=0.1)
    assert len(res.clean) == len(y) and Path(rec.session.clean).exists()
    lights = [l for _, l, _ in st]
    assert "amber" in lights and "red" in lights and "clear" in lights
    assert any(e.kind == "red" for e in res.ear_events)


def test_pause_discards_audio_and_take_time(tmp_path):
    x, _ = speech_take(60.0, -24, seed=81)
    rec, res, st = record(x, tmp_path, realtime=False, pause_at=(20.0, 30.0))
    raw, _ = sf.read(res.raw_path, dtype="float32")
    assert res.duration_s < 60.0 - 5.0                      # ~10 s were paused out
    assert len(raw) == int(round(res.duration_s * SR))
    assert any(p for _, _, p in st)


def test_callback_never_blocks_when_worker_lags(tmp_path):
    """A full queue drops blocks (counted) instead of blocking the audio callback."""
    rec = Recorder(Session(tmp_path / "s"), source=ArraySource(np.zeros(SR, np.float32)), use_radar=False)
    rec.q.maxsize = 2
    for _ in range(10):
        rec._enqueue(np.zeros(BLOCK, np.float32))
    assert rec.status.dropped_blocks == 8
    rec._resolve_losses()                                   # worker not started: resolve by hand
    rec._consumed = 2                                       # it caught up with the 2 accepted blocks
    rec._resolve_losses()
    assert rec.status.lost_at_s == [0.0]                    # T8: where, once per 0.5 s
    rep = rec.capture_report()
    assert rep["lost_at_s"] == [0.0] and rep["lost_buffers"] == 8   # total kept apart from spots


def test_lost_spot_time_is_where_it_happened_not_when_processed(tmp_path):
    """T8: with the worker 3 s behind, a drop is stamped at its own take position."""
    rec = Recorder(Session(tmp_path / "s"), source=ArraySource(np.zeros(SR, np.float32)), use_radar=False)
    rec.q.maxsize = 30
    for _ in range(30):                                     # 3.0 s accepted, worker idle
        rec._enqueue(np.zeros(BLOCK, np.float32))
    rec._enqueue(np.zeros(BLOCK, np.float32))               # queue full: dropped at 3.0 s
    rec.source.overflows = 1                                # driver loses sound before the next block
    rec.q.get_nowait(); rec._consumed += 1                  # one block consumed → room again
    rec._samples = 30 * BLOCK                               # worker has now processed to 3.0 s
    rec._enqueue(np.zeros(BLOCK, np.float32))
    rec._consumed = 31; rec._resolve_losses()
    assert rec.status.dropped_blocks == 1
    assert rec.status.lost_at_s == [pytest.approx(3.0)]     # both losses sit at the 3.0 s boundary


@pytest.mark.skipif(not REAL_RAW.exists(), reason="Sam's raw take not present")
def test_background_deepfilternet_segments_cover_the_take(tmp_path):
    from core.denoise import deepfilter_exe
    from core.session import load_mono_48k
    if deepfilter_exe() is None:
        pytest.skip("DeepFilterNet not installed")
    x = load_mono_48k(REAL_RAW)[: 150 * SR]
    rec, res, _ = record(x, tmp_path, segment_s=40.0)
    assert res.backend == "DeepFilterNet"
    assert len(rec._futures) >= 3                           # ≥ 2 finished in the background + the tail
    assert len(res.clean) == len(x)
    from core.diagnostics import zero_gaps
    assert zero_gaps(res.clean) == []                       # T7: no dropouts at segment joins


def test_listen_back_returns_last_seconds_cleaned(tmp_path):
    x, _ = speech_take(40.0, -24, seed=82)
    rec, res, _ = record(x, tmp_path)
    y = rec.listen_back(15.0)
    assert len(y) == 15 * SR
    # it's the end of the take (aligned), and not louder than the input
    assert np.sqrt(np.mean(y ** 2)) <= np.sqrt(np.mean(x[-15 * SR:] ** 2)) * 1.05


def test_capture_report_counts_clipping_and_is_saved(tmp_path):
    """T7: a too-loud mic must be visible (status + capture.json), not silently baked in."""
    import json
    x, _ = speech_take(15.0, -24, seed=83)
    x = (x / np.abs(x).max() * 1.5).astype(np.float32)          # float input past full scale
    rec, res, _ = record(x, tmp_path)
    assert rec.status.clipped_samples > 0 and rec.status.over_full_scale > 0
    rep = json.loads((rec.session.dir / "capture.json").read_text("utf-8"))
    assert rep["clipped_samples"] == rec.status.clipped_samples
    assert rep["over_full_scale_samples"] > 0 and rep["take_s"] == pytest.approx(15.0, abs=0.1)
    assert rep["peak_dbfs"] > 3.0


def test_clean_take_reports_no_clipping(tmp_path):
    x, _ = speech_take(10.0, -24, seed=84)
    rec, _, _ = record(x.astype(np.float32), tmp_path)
    assert rec.status.clipped_samples == 0 and rec.status.over_full_scale == 0
