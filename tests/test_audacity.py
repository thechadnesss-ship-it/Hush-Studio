import numpy as np
import soundfile as sf

from core.audacity import export_for_audacity, label_rows
from core.edl import Cut, EditList, Marker, Pause


def test_label_track_contains_edits_markers_and_playhead(tmp_path):
    sr = 1000
    edl = EditList(duration=10.0, sample_rate=sr,
                   cuts=[Cut(1.0, 2.0, "manual")],
                   pauses=[Pause(3.0, 4.0, 5.0, kind="marker")],
                   markers=[Marker(7.0, "remember this")])
    rows = label_rows(edl, playhead_s=6.25)
    labels = [label for _, _, label in rows]
    assert any(label.startswith("CUT:") for label in labels)
    assert "PAUSE: marker" in labels
    assert "MARKER: remember this" in labels
    assert "PLAYHEAD" in labels
    assert all(0 <= start <= end <= edl.output_duration() for start, end, _ in rows)


def test_export_writes_audio_and_audacity_label_sidecar(tmp_path):
    sr = 1000
    edl = EditList(duration=2.0, sample_rate=sr, markers=[Marker(0.5, "start")])
    audio = np.linspace(-0.5, 0.5, 2 * sr, dtype=np.float32)
    wav, labels = export_for_audacity(tmp_path / "for_audacity.wav", audio, edl, 1.25)

    restored, rate = sf.read(wav, dtype="float32")
    assert rate == sr and np.array_equal(restored, audio)
    lines = labels.read_text("utf-8").splitlines()
    assert any(line.endswith("\tMARKER: start") for line in lines)
    assert any(line.endswith("\tPLAYHEAD") for line in lines)
