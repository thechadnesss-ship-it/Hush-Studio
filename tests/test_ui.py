"""UI smoke tests (offscreen): the window builds, reflects the guard live, and the review
dialog edits the edit list. Screenshots go to .work/ui_*.png for a visual check."""
import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["HUSH_NO_PLANNER"] = "1"            # no live radar / real sky database in tests
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")    # offscreen has no fonts otherwise

import numpy as np
import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QApplication

from core.capture import ArraySource, Recorder
from core.edl import Cut, EditList, Pause
from core.session import Session
from synth import SR, plane, speech_take
from ui import theme

SHOTS = Path(__file__).resolve().parent.parent / ".work"


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    theme.load_fonts()
    a.setStyleSheet(theme.stylesheet())
    return a


def pump(app, seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.01)


def test_window_reflects_live_guard(app, tmp_path):
    from ui.main_window import MainWindow
    w = MainWindow()
    w.resize(900, 640)
    w.show()
    pump(app, 0.2)
    SHOTS.mkdir(exist_ok=True)
    w.grab().save(str(SHOTS / "ui_idle.png"))
    assert w.btn_rec.isEnabled() and not w.btn_finish.isEnabled()

    # drive a recording from an array (a plane flies over at 100 s)
    x, _ = speech_take(160.0, -24, seed=90)
    y = (x + plane(160.0, 100.0, -30)).astype(np.float32)
    src = ArraySource(y)
    w.session = Session(tmp_path / "s")
    w.recorder = Recorder(w.session, source=src, use_radar=False)
    w._set_mode("recording")
    w.poll.start()
    lights_seen = set()
    w.recorder.start()
    while not src.done.is_set() or not w.recorder.q.empty():
        pump(app, 0.05)
        lights_seen.add(w.light.state)
        if w.light.state == "red" and not (SHOTS / "ui_red.png").exists():
            w.grab().save(str(SHOTS / "ui_red.png"))
    w.poll.stop()
    w.recorder.stop()
    assert "red" in lights_seen
    assert w.timer.text().startswith("00:02:")
    w.close()


def test_review_dialog_edits_the_list(app):
    from ui.review import ReviewDialog
    clean = np.zeros(SR * 120, np.float32)
    edl = EditList(duration=120.0, sample_rate=SR,
                   cuts=[Cut(40.0, 62.0, "plane")],
                   pauses=[Pause(10.0, 14.0, 10.0, kind="marker"), Pause(80.0, 83.0, 10.0, kind="marker"),
                           Pause(20.0, 21.0, 1.0)])
    d = ReviewDialog(edl, clean)
    d.resize(760, 560)
    d.show()
    pump(app, 0.2)
    d.grab().save(str(SHOTS / "ui_review.png"))
    assert "1 plane cut" in d.summary.text() and "2 pause markers" in d.summary.text()
    before = edl.output_duration()
    # "Keep anyway" on the plane cut, shorter markers, un-mark one marker
    from PyQt6.QtWidgets import QCheckBox
    d.cuts.cellWidget(0, 3).findChild(QCheckBox).setChecked(False)
    assert edl.cuts[0].keep is True
    d.marker_len.setValue(6.0)
    assert all(p.new_length == 6.0 for p in edl.pauses if p.kind == "marker")
    d.markers.cellWidget(1, 2).findChild(QCheckBox).setChecked(False)
    assert sum(p.kind == "marker" for p in edl.pauses) == 1
    assert edl.output_duration() != before
    d.close()


def test_sky_forecast_dialog(app):
    from datetime import datetime
    from core.quiet_planner.forecast import build
    from test_quiet_planner import synth_history
    from ui.sky import SkyDialog
    passes, polls = synth_history(days=9)
    d = SkyDialog(build(passes, polls), now=datetime(2026, 11, 12, 9, 0))
    d.show()
    pump(app, 0.2)
    d.grab().save(str(SHOTS / "ui_sky.png"))
    assert [w.start.hour for w in d.windows if w.expected < 0.5] == [13, 22]
    d.close()


def test_app_name_is_used(app):
    from core.app_info import APP_NAME
    from ui.main_window import MainWindow
    w = MainWindow()
    assert w.windowTitle() == APP_NAME == "Hush Studio"
    w.close()


def test_publish_dialog(app, tmp_path, monkeypatch):
    from core import youtube_publisher as yt
    from ui.publish import PublishDialog
    video = tmp_path / "output_8h.mp4"
    video.write_bytes(b"0" * 1024)
    monkeypatch.setattr(yt, "CLIENT_SECRET", tmp_path / "missing.json")
    d = PublishDialog(video, 8)
    d.show()
    pump(app, 0.2)
    d.grab().save(str(SHOTS / "ui_publish_setup.png"))
    assert not d.ok.isEnabled() and d.setup.isVisible()           # guides the one-time setup
    secret = tmp_path / "client.json"
    secret.write_text("{}")
    monkeypatch.setattr(yt, "CLIENT_SECRET", secret)
    d._update()
    d.hook.setText("Your Mind Has Been Loud All Day… Let It Finally Go Quiet")
    d.schedule.setChecked(True)
    pump(app, 0.2)
    d.grab().save(str(SHOTS / "ui_publish.png"))
    assert d.ok.isEnabled() and not d.setup.isVisible()
    assert d.preview.text().startswith("Your Mind Has Been Loud All Day… Let It Finally Go Quiet | Deep Sleep")
    d._accept()
    assert d.meta is not None and d.meta.publish_at is not None
    d.close()
