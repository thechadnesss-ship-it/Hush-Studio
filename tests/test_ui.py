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


def test_review_flub_proposals_accept_undo_and_render(app):
    from PyQt6.QtWidgets import QPushButton
    from ui.review import ReviewDialog
    clean = np.random.default_rng(1).normal(0, 0.1, SR * 60).astype(np.float32)
    edl = EditList(duration=60.0, sample_rate=SR, cuts=[
        Cut(10.0, 15.0, "flub", keep=True, proposed=True), Cut(30.0, 33.0, "flub", keep=True, proposed=True)])
    full = edl.output_duration()
    d = ReviewDialog(edl, clean)
    d.show()
    pump(app, 0.1)
    assert "2 waiting" in d.summary.text() and edl.output_duration() == full   # proposals are not applied
    d.cuts.cellWidget(0, 4).click()                                            # preview must not crash
    next(b for b in d.cuts.cellWidget(0, 3).findChildren(QPushButton) if b.text() == "Accept cut").click()
    assert not edl.cuts[0].keep and not edl.cuts[0].proposed
    assert edl.output_duration() < full - 4
    next(b for b in d.cuts.cellWidget(1, 3).findChildren(QPushButton) if b.text() == "Undo").click()
    assert edl.cuts[1].keep and not edl.cuts[1].proposed
    assert len(edl.render(clean)) / SR == edl.output_duration() or abs(len(edl.render(clean)) / SR - edl.output_duration()) < 0.05
    assert "waiting" not in d.summary.text()
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


def _record_into_window(app, w, tmp_path, x):
    src = ArraySource(np.asarray(x, np.float32), realtime=True)
    w.session = Session(tmp_path / "s")
    w.recorder = Recorder(w.session, source=src, use_radar=False)
    w._reset_mic_warning()
    w._set_mode("recording")
    w.poll.start()
    w.recorder.start()
    seen = False
    while not src.done.is_set():
        pump(app, 0.05)
        seen = seen or (w.mic_warning.isVisible() and w.vu.clipping)
    pump(app, 0.3)
    return seen


def test_mic_clipping_warning(app, tmp_path):
    """T7: a too-loud mic turns the meter red and says what to do."""
    from ui.main_window import MIC_CLIP_WARNING, MainWindow
    w = MainWindow()
    w.resize(900, 640)
    w.show()
    x, _ = speech_take(4.0, -24, seed=95)
    hot = np.clip(x / np.abs(x).max() * 3.0, -1.0, 1.0)            # an overdriven mic
    assert _record_into_window(app, w, tmp_path, hot)
    assert w.mic_warning.text() == MIC_CLIP_WARNING
    SHOTS.mkdir(exist_ok=True)
    w.grab().save(str(SHOTS / "ui_clipping.png"))
    w.poll.stop()
    w.recorder.stop()
    w.recorder = None


def test_clean_take_shows_no_mic_warning(app, tmp_path):
    from ui.main_window import MainWindow
    w = MainWindow()
    w.show()
    x, _ = speech_take(3.0, -24, seed=96)
    assert not _record_into_window(app, w, tmp_path, x)
    assert not w.mic_warning.isVisible() and not w.vu.clipping
    w.poll.stop()
    w.recorder.stop()
    w.recorder = None


def _review(capture):
    from ui.review import ReviewDialog
    edl = EditList(duration=60.0, sample_rate=SR)
    return ReviewDialog(edl, np.zeros(60 * SR, np.float32), capture=capture)


def test_review_shows_lost_sound_before_render(app):
    """T8: a take that lost sound while recording is obvious in review, with ▶ per spot."""
    from PyQt6.QtWidgets import QPushButton
    cap = {"input_overflows": 2, "dropped_blocks": 1, "clipped_samples": 0, "lost_at_s": [12.3, 40.0]}
    dlg = _review(cap)
    dlg.show()
    pump(app, 0.1)
    assert not dlg.check.isHidden() and "lost 3 times in 2 spots" in dlg.check.text()
    spots = [dlg.lost_row.itemAt(i).widget() for i in range(dlg.lost_row.count())]
    assert [b.text() for b in spots if isinstance(b, QPushButton)] == ["▶  0:12", "▶  0:40"]
    SHOTS.mkdir(exist_ok=True)
    dlg.grab().save(str(SHOTS / "ui_review_lost.png"))


def test_review_has_a_play_button_for_every_lost_spot(app):
    """T8 review round 2: all recorded spots (up to the 200 cap) are playable, none hidden behind '+N more'."""
    from PyQt6.QtWidgets import QPushButton
    from core.capture import MAX_LOST_SPOTS
    times = [float(i * 3) for i in range(MAX_LOST_SPOTS)]
    dlg = _review({"input_overflows": 0, "dropped_blocks": 0, "lost_buffers": 500,
                   "clipped_samples": 0, "lost_at_s": times})
    dlg.show()
    pump(app, 0.1)
    btns = [dlg.lost_row.itemAt(i).widget() for i in range(dlg.lost_row.count())]
    btns = [b for b in btns if isinstance(b, QPushButton)]
    assert len(btns) == MAX_LOST_SPOTS and not dlg.lost_strip.isHidden()
    assert "lost 500 times" in dlg.check.text() and "scroll" in dlg.check.text()


def test_review_check_for_a_clean_take_and_an_import(app):
    ok = _review({"input_overflows": 0, "dropped_blocks": 0, "clipped_samples": 0, "lost_at_s": []})
    assert ok.check.text() == "Recording check: no lost sound, no clipping." and not ok.check.isHidden()
    clip = _review({"input_overflows": 0, "dropped_blocks": 0, "clipped_samples": 1500, "lost_at_s": []})
    assert "clipped (1,500 samples)" in clip.check.text() and "lost" not in clip.check.text()
    assert _review(None).check.isHidden()                 # imported take: no capture data


def test_quality_worker_emits_report_and_prepare_worker_keeps_three_args(app, monkeypatch):
    from core.audio_quality import QualityReport
    from core.config import RenderConfig
    from ui.workers import AudioQualityWorker, PrepareWorker
    import ui.workers as workers

    x, _ = speech_take(1.0, -24, seed=410)
    reports = []
    worker = AudioQualityWorker(x, x)
    worker.done.connect(reports.append)
    worker.start()
    while worker.isRunning():
        pump(app, 0.02)
    pump(app, 0.05)
    assert len(reports) == 1 and isinstance(reports[0], QualityReport)

    edl = EditList(duration=1.0, sample_rate=SR)
    monkeypatch.setattr(workers, "prepare", lambda *args, **kwargs: edl)
    session = Session(Path.cwd())
    recording = type("Recording", (), {"clean": x, "ear_events": []})()
    args = []
    prep = PrepareWorker(session, recording=recording, cfg=RenderConfig())
    prep.done.connect(lambda *values: args.append(values))
    prep.start()
    while prep.isRunning():
        pump(app, 0.02)
    pump(app, 0.05)
    assert len(args) == 1 and len(args[0]) == 3


def test_takes_dialog_lists_and_manages(app, tmp_path):
    from core.takes import TakeLibrary
    from ui.takes import TakesDialog
    import soundfile as sf
    lib = TakeLibrary(tmp_path / "Takes")
    for n in ("Alpha", "Beta"):
        d = lib.new_dir(n)
        sf.write(d / "raw.flac", np.zeros(4800, np.float32), 48000)
    dlg = TakesDialog(lib)
    assert dlg.table.rowCount() == 2
    dlg.search.setText("alp")
    assert dlg.table.rowCount() == 1 and dlg.table.item(0, 0).text() == "Alpha"
    dlg.table.selectRow(0)
    dlg.duplicate()
    assert any(t.name == "Alpha (copy)" for t in lib.list())
    dlg.search.setText("")
    assert dlg.table.rowCount() == 3


# ----------------------------------------------------------------- T12: waveform editor

def _hash(p):
    import hashlib
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _wait_job(app, d, timeout=120):
    end = time.perf_counter() + timeout
    while d._job is not None and time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.02)
    assert d._job is None, "editor job did not finish"


def _drag(view, a_s, b_s):
    from PyQt6.QtCore import QPoint
    from PyQt6.QtTest import QTest
    y = view.height() // 2
    xa, xb = int(view.x_of(a_s)), int(view.x_of(b_s))
    QTest.mousePress(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(xa, y))
    QTest.mouseMove(view, QPoint((xa + xb) // 2, y))
    QTest.mouseMove(view, QPoint(xb, y))
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(xb, y))


from PyQt6.QtCore import Qt  # noqa: E402


def test_editor_hand_edits_undo_automatic_tools_and_raw_untouched(app, tmp_path, monkeypatch):
    """Open a take, cut / silence / undo by hand, run the automatic tools on part of the take and
    on all of it, export for Audacity: edits.json follows along, raw.flac and clean.wav never change."""
    import soundfile as sf
    from ui.editor import EditorDialog
    x, _ = speech_take(150.0, -24, seed=21)
    raw = (x + plane(150.0, t_peak=100.0, peak_db=-30)).astype(np.float32)
    take = tmp_path / "take"
    take.mkdir()
    sf.write(take / "raw.flac", raw, SR, format="FLAC", subtype="PCM_24")
    sf.write(take / "clean.wav", x, SR, subtype="FLOAT")
    h_raw, h_clean = _hash(take / "raw.flac"), _hash(take / "clean.wav")
    edl = EditList(duration=150.0, sample_rate=SR, cuts=[Cut(20.0, 22.0, "flub", keep=True, proposed=True)])
    d = EditorDialog(take, edl, x)
    d.resize(1240, 760)
    d.show()
    pump(app, 0.3)
    v = d.view
    assert v.spp * v.wave_w >= 150.0                      # opens showing the whole take
    assert d.act_next.isVisible() and "(1)" in d.act_next.text()
    SHOTS.mkdir(exist_ok=True)
    d.grab().save(str(SHOTS / "ui_editor.png"))

    # select by dragging, delete, undo, redo
    _drag(v, 40.0, 45.0)
    assert v.sel and abs(v.sel[0] - 40.0) < 0.5 and abs(v.sel[1] - 45.0) < 0.5
    assert d.act_delete.isEnabled()
    a, b = v.sel
    d.act_delete.trigger()
    assert edl.cuts[-1].reason == "manual" and abs(edl.cuts[-1].start - a) < 1e-3
    assert edl.output_duration() == pytest.approx(150.0 - (b - a) - 0.01, abs=0.01)
    pump(app, 0.6)
    assert EditList.load(take / "edits.json").cuts[-1].reason == "manual"   # saved as she goes
    d.act_undo.trigger()
    assert [c.reason for c in edl.cuts] == ["flub"]
    d.act_redo.trigger()
    assert len(edl.cuts) == 2

    # silence + insert silence + copy/paste + fade + marker, via the same handlers as the buttons
    d.select(60.0, 62.0)
    d.act_silence.trigger()
    assert edl.pauses and edl.pauses[-1].kind == "silence"
    d.select(70.0, 71.0)
    d.act_copy.trigger()
    d.move_cursor(90.0)
    d.act_paste.trigger()
    assert len(edl.inserts) == 1
    d.select(100.0, 104.0)
    d.act_fade_out.trigger()
    d.move_cursor(110.0)
    d.act_label_sel.trigger()
    assert len(edl.markers) == 1 and d.goto_menu.isEnabled()

    # the flub proposal: jump to it and accept it
    d.next_proposal()
    flub = edl.cuts[0]
    d.editor.decide_cut(flub.id, "accept")
    d._after_edit(label="Cut accepted")
    assert not flub.keep and not d.act_next.isVisible()

    # Audacity bits: typed selection times, Trim, dB view, vertical zoom, quick-play from the timeline
    d.sel_mode.setCurrentIndex(1)                          # Start and Length
    d.f_start.set_seconds(30.0)
    d.f_end.set_seconds(2.5)
    d._sel_typed()
    assert v.sel == pytest.approx((30.0, 32.5))
    d.sel_mode.setCurrentIndex(0)
    assert d.f_end.seconds() == pytest.approx(32.5, abs=1e-3)
    before = edl.output_duration()
    d.act_trim.trigger()
    assert edl.output_duration() == pytest.approx(2.5, abs=0.05)
    d.act_undo.trigger()
    assert edl.output_duration() == pytest.approx(before, abs=1e-6)
    d.act_db.trigger()
    v.repaint()
    assert v.db_view and d.act_db.isChecked()
    d.act_linear.trigger()
    v._vzoom(0.5)
    v.reset_vzoom()
    d.quick_play(10.0)                                     # no sound device needed for this to be safe
    d.stop_play()
    d.nudge(1)
    d.nudge(1, extend=True)
    assert v.sel is not None

    # zoom down to single samples and back: every drawing path
    d.view.show_range(30.0, 30.01)
    v.repaint()
    d.view.show_range(30.0, 31.0)
    v.repaint()
    d.view.fit()

    # click-menus on a cut ▼ and a marker flag build without blocking; deleting everything is survivable
    from PyQt6.QtWidgets import QMenu
    shown = []
    with monkeypatch.context() as mp:
        mp.setattr(QMenu, "exec", lambda self, *a: shown.append([act.text() for act in self.actions()]))
        d._item_clicked("cut", next(m for m in v._marks if m.active))
        d._item_clicked("marker", v._markers[0])
        d._context_menu(v.mapToGlobal(v.rect().center()))
    assert any("Restore (keep this audio)" in t for t in shown[0]) and "Rename…" in shown[1] and any("Silence" in x for x in shown[2])
    d.select_all()
    d.act_delete.trigger()
    v.repaint()
    assert edl.output_duration() == 0
    d.act_undo.trigger()
    assert edl.output_duration() > 100

    # automatic tools: part of the take, then all of it
    d.select(*[v.ed.tl.src_to_out(t * SR)[0] / SR for t in (60.0, 130.0)])
    d.act_planes.trigger()
    _wait_job(app, d)
    planes = [c for c in edl.cuts if c.reason == "plane"]
    assert len(planes) == 1 and planes[0].start < 85 and planes[0].end > 115, d.status.text()
    d.clear_selection()
    n_locked = sum(p.locked for p in edl.pauses)
    d.act_auto.trigger()
    _wait_job(app, d)
    assert sum(not p.locked for p in edl.pauses) > 5 and sum(p.locked for p in edl.pauses) == n_locked
    d.act_norm.trigger()
    _wait_job(app, d)
    assert any(g.reason == "normalise" for g in edl.gains)
    steps = len(d.editor._undo)
    d.act_undo.trigger()                                     # each automatic run is one undo step
    assert not any(g.reason == "normalise" for g in edl.gains) and len(d.editor._undo) == steps - 1

    # Open in Audacity: exports the edited take (Audacity itself may not be installed here)
    import ui.editor as editor_mod
    launched = []
    monkeypatch.setattr(editor_mod, "find_audacity", lambda: "audacity.exe")
    monkeypatch.setattr(editor_mod.subprocess, "Popen", lambda args: launched.append(args))
    d.act_aud_open.trigger()
    _wait_job(app, d)
    out, _ = sf.read(take / "for_audacity.wav", dtype="float32")
    assert len(out) == pytest.approx(edl.output_duration() * SR, abs=2) and launched

    # the result renders, and the recorded audio is exactly as it was
    assert len(edl.render(x)) == pytest.approx(edl.output_duration() * SR, abs=2)
    d.grab().save(str(SHOTS / "ui_editor_edited.png"))
    d.reject()
    assert EditList.load(take / "edits.json") == edl
    assert _hash(take / "raw.flac") == h_raw and _hash(take / "clean.wav") == h_clean


def test_review_edit_by_hand_opens_the_editor(app, tmp_path, monkeypatch):
    import soundfile as sf
    from ui import editor as editor_mod
    from ui import review as review_mod
    from ui.main_window import MainWindow
    w = MainWindow()
    take = tmp_path / "t"
    take.mkdir()
    clean = np.zeros(SR * 30, np.float32)
    sf.write(take / "clean.wav", clean, SR, subtype="FLOAT")
    edl = EditList(30.0, SR)
    edl.save(take / "edits.json")
    w.session = Session(take)
    opened = []
    monkeypatch.setattr(review_mod.ReviewDialog, "exec", lambda self: review_mod.ReviewDialog.EDIT)
    monkeypatch.setattr(editor_mod.EditorDialog, "exec", lambda self: opened.append(self) or 0)
    w._prepared(edl, clean)
    assert len(opened) == 1 and w.mode == "done"
    w.reopen_take(take, "review")                         # Takes → "Open in editor"
    assert len(opened) == 2 and opened[1].editor.edl.duration == 30.0
    w.close()


def test_edit_button_after_a_take_then_finish_renders_the_edit(app, tmp_path, monkeypatch):
    import soundfile as sf
    from ui import review as review_mod
    from ui import editor as editor_mod
    from ui.main_window import MainWindow
    w = MainWindow()
    take = tmp_path / "t"
    take.mkdir()
    clean = np.zeros(SR * 30, np.float32)
    sf.write(take / "clean.wav", clean, SR, subtype="FLOAT")
    edl = EditList(30.0, SR)
    edl.save(take / "edits.json")
    w.session = Session(take)
    assert not w.btn_edit.isVisibleTo(w)
    monkeypatch.setattr(review_mod.ReviewDialog, "exec", lambda self: 0)        # dismiss the review
    w._prepared(edl, clean)
    assert w.mode == "done" and w.btn_edit.isVisibleTo(w) and w.btn_finish.isEnabled()

    def fake_exec(self):                                                         # make a change in the editor
        self.editor.edl.cuts.append(Cut(5.0, 10.0, reason="manual"))
        return 0
    monkeypatch.setattr(editor_mod.EditorDialog, "exec", fake_exec)
    rendered = []
    monkeypatch.setattr(w, "render", lambda: rendered.append([(c.start, c.end) for c in w.edl.cuts]))
    w.btn_edit.click()
    w.btn_finish.click()
    assert rendered == [[(5.0, 10.0)]]
    w.close()
