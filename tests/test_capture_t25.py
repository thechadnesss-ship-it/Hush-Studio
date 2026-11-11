"""T25 regression: live DeepFilterNet segments share one staging gain."""
import numpy as np
import pytest

from core.capture import Recorder
from core.denoise import deepfilter_exe, deepfilter_input_gain_db
from core.session import Session
from synth import SR


@pytest.mark.skipif(deepfilter_exe() is None, reason="needs DeepFilterNet")
def test_live_dfn_segments_use_one_stage_gain(monkeypatch, tmp_path):
    calls = []

    def fake_deepfilter(x, sr, attenuation_limit_db, workers, work_dir, context,
                        stage_gain_db=None):
        calls.append(stage_gain_db)
        return np.zeros(len(x), dtype=np.float32)

    monkeypatch.setattr("core.capture.deepfilter_array", fake_deepfilter)
    rec = Recorder(Session(tmp_path / "s"), use_radar=False)
    try:
        first = np.full(SR, 10 ** (-30 / 20), dtype=np.float32)
        second = np.full(SR, 10 ** (-10 / 20), dtype=np.float32)
        rec._pending = [first]
        rec._pending_len = len(first)
        rec._submit_segment()
        rec._pending = [second]
        rec._pending_len = len(second)
        rec._submit_segment()
        for future in rec._futures:
            future.result()
        assert calls == [calls[0], calls[0]]
        assert calls[0] == deepfilter_input_gain_db(first)
    finally:
        rec._pool.shutdown(wait=True)
