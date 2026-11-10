"""T20 debug bundle: contents, no audio by default, opt-in clips, same report when read back."""
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np
import soundfile as sf

from core import debug_bundle as db
from core.diagnostics import format_report, stage_report
from synth import SR, speech_take

ROOT = Path(__file__).resolve().parent.parent


def _session(tmp_path):
    x, regions = speech_take(20.0, -24, seed=95)
    x = x.astype(np.float32)
    s, e = regions[1]
    mid = int((s + e) / 2 * SR)
    x[mid:mid + 1440] = 0                         # a dropout
    d = tmp_path / "sessions" / "2026-11-10_120000"
    d.mkdir(parents=True)
    sf.write(d / "raw.flac", x, SR, format="FLAC", subtype="PCM_24")
    sf.write(d / "clean.wav", x, SR, subtype="FLOAT")
    (d / "capture.json").write_text(json.dumps({"device": "Test mic", "clipped_samples": 0, "lost_at_s": []}))
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "hush-studio.log").write_text("INFO ok token=abc123secret\n")
    return d, logs


def test_default_bundle_has_no_audio_and_the_right_files(tmp_path):
    d, logs = _session(tmp_path)
    z = db.build_bundle(d, tmp_path / "out", {"mic": "USB", "youtube_token": "ya29.x", "client_secret": "s"}, logs=logs)
    names = zipfile.ZipFile(z).namelist()
    assert not [n for n in names if n.startswith("audio/") or n.endswith((".flac", ".wav", ".m4a", ".mp4", ".mp3"))]
    for need in ("system.json", "settings.json", "logs/hush-studio.log", "take/capture.json",
                 "take/report.json", "take/report.txt", "manifest.json", "README.txt"):
        assert need in names
    zf = zipfile.ZipFile(z)
    st = zf.read("settings.json").decode()
    assert "USB" in st and "ya29" not in st and "client_secret" not in st and "youtube_token" not in st
    assert "abc123secret" not in zf.read("logs/hush-studio.log").decode()
    assert str(tmp_path) not in zf.read("take/report.json").decode()


def test_clips_only_when_ticked(tmp_path):
    d, logs = _session(tmp_path)
    spots = db.flagged_spots(d)
    assert spots and spots[0]["kind"] == "dropout"
    z = db.build_bundle(d, tmp_path / "out", {}, clip_ids=[spots[0]["id"]], logs=logs)
    zf = zipfile.ZipFile(z)
    clips = [n for n in zf.namelist() if n.startswith("audio/")]
    assert clips == [f"audio/{spots[0]['id']}.flac"]
    zf.extract(clips[0], tmp_path / "x")
    assert sf.info(tmp_path / "x" / clips[0]).duration <= 6.1


def test_bundle_reads_like_a_session(tmp_path):
    d, logs = _session(tmp_path)
    z = db.build_bundle(d, tmp_path / "b.zip", {}, logs=logs)
    local = format_report(stage_report(d)).replace(str(d), d.name)
    out = subprocess.run([sys.executable, "-m", "core.diagnostics", str(z)], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == local.strip()


def test_save_locations_always_offers_a_folder():
    assert db.save_locations() is not None   # flash drives (if any) come first; never raises


def test_dialog_nothing_ticked_and_saves_without_audio(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from ui import debug_report as dr
    monkeypatch.setattr(dr, "open_folder", lambda p: None)
    monkeypatch.setattr(db, "save_locations", lambda: [("Test folder", tmp_path / "out")])
    d, logs = _session(tmp_path)
    monkeypatch.setattr(db, "log_dir", lambda: logs)
    monkeypatch.setattr(db, "build_bundle", (lambda orig: lambda *a, **k: orig(*a, logs=logs, **k))(db.build_bundle))
    dlg = dr.DebugReportDialog([d], d, {"mic": "USB"})
    assert dlg.spots and dlg.ticked() == []              # clips offered, none ticked
    dlg.make()                                            # click 3: Save
    assert dlg.result_path.exists()
    assert not [n for n in zipfile.ZipFile(dlg.result_path).namelist() if n.startswith("audio/")]
    assert "no audio" in dlg.status.text()
    dlg.ticks[dlg.spots[0]["id"]].setChecked(True)
    dlg.make()
    assert any(n.startswith("audio/") for n in zipfile.ZipFile(dlg.result_path).namelist())
