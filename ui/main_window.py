"""Main window (CLAUDE.md Module A).

  header : target length · status pill
  middle : traffic light (+ window border glow) · take timer · plan preview · VU · companion
  bottom : ⏺ REC · ⏸ PAUSE · ⏹ FINISH & RENDER · Import take… · settings

Flow: REC → (guard live) → FINISH → review edits → render → output_<h>h.mp4.
      Import take… → noise removal + planes + pauses → review → render.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QSettings, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow,
                             QMenu, QMessageBox, QProgressBar, QPushButton, QVBoxLayout, QWidget)

from core.app_info import APP_NAME, SETTINGS_ORG
from core.asset_cache import AssetCache
from core.config import DATA_ROOT, PROJECT_ROOT, TARGET_HOURS_OPTIONS, load_render_config
from core.renderer import TakeTooShortError, fmt_hms, fmt_mmss, plan_render
from core.session import Session
from core.takes import TakeLibrary, migrate_sessions_folder

from . import theme
from .review import ReviewDialog
from .widgets import Companion, RecordButton, TrafficLight, VUMeter
from .workers import PrepareWorker, RenderWorker

SR = 48000
CLIP_DB = -0.01                    # peak at full scale (16-bit max is −0.0003 dBFS)
MIC_WARN_HOLD_S = 6.0
MIC_CLIP_WARNING = ("Mic too loud: it's clipping, which sounds harsh. Turn the mic level down "
                    "(Windows Sound settings → Recording → your mic → Properties → Levels).")
MIC_DROP_WARNING = "Some sound was lost: the computer was too busy. Close other apps if it keeps happening."
TAKES_DIR = migrate_sessions_folder(DATA_ROOT)      # D04: one folder per take, visible in Explorer
SESSIONS_DIR = TAKES_DIR
ASSETS_DIR = PROJECT_ROOT / "assets"


def default_intro_card() -> Path | None:
    for p in [ASSETS_DIR / "intro_card.mp4", *sorted(PROJECT_ROOT.glob("*Intro*.mp4"))]:
        if p.exists():
            return p
    return None


def chime(freqs=(660.0, 880.0), dur=0.18, level_db=-22.0) -> None:
    """Soft two-note chime to her headphones only (output device), never into the recording."""
    try:
        import sounddevice as sd
        t = np.arange(int(dur * SR)) / SR
        env = np.sin(np.pi * t / dur) ** 2
        tone = np.concatenate([np.sin(2 * np.pi * f * t) * env for f in freqs]) * 10 ** (level_db / 20)
        sd.play(tone.astype(np.float32), SR)
    except Exception:
        pass


class FuncWorker(QThread):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception:
            self.failed.emit(traceback.format_exc())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(900, 640)
        self.settings = QSettings(SETTINGS_ORG, APP_NAME)
        self.cfg = load_render_config()
        self.assets = AssetCache(self.cfg)
        self.recorder = None
        self.library = TakeLibrary(TAKES_DIR)
        self.session: Session | None = None
        self.edl = None
        self.clean = None
        self._workers: list[QThread] = []
        self.output: Path | None = None
        self.intro_card = Path(self.settings.value("intro_card", "")) if self.settings.value("intro_card") else default_intro_card()

        root = QWidget()
        self.frame = QFrame()
        self.frame.setObjectName("frame")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.frame)
        lay = QVBoxLayout(self.frame)
        lay.setContentsMargins(32, 22, 32, 26)
        lay.setSpacing(0)
        self.setCentralWidget(root)

        # --- top bar: brand · video length · settings
        head = QHBoxLayout()
        brand = QLabel(APP_NAME)
        brand.setObjectName("brand")
        head.addWidget(brand)
        head.addStretch(1)
        lbl = QLabel("Video length")
        lbl.setObjectName("muted")
        head.addWidget(lbl)
        self.target = QComboBox()
        for h in TARGET_HOURS_OPTIONS:
            self.target.addItem(f"{h} hours" if h > 1 else "1 hour", h)
        self.target.setCurrentIndex(self.target.findData(int(self.settings.value("target_hours", 8))))
        self.target.currentIndexChanged.connect(self._target_changed)
        head.addWidget(self.target)
        self.btn_takes = QPushButton("Takes")
        self.btn_takes.setObjectName("link")
        self.btn_takes.clicked.connect(self.show_takes)
        head.addWidget(self.btn_takes)
        self.btn_settings = QPushButton("Settings")
        self.btn_settings.setObjectName("link")
        self.btn_settings.clicked.connect(lambda: self.panel.setVisible(not self.panel.isVisible()))
        head.addWidget(self.btn_settings)
        self.btn_help = QPushButton("Help")
        self.btn_help.setObjectName("link")
        self.help_menu = QMenu(self.btn_help)
        self.act_debug = self.help_menu.addAction("Send debug report…")
        self.act_debug.triggered.connect(self.send_debug_report)
        self.btn_help.setMenu(self.help_menu)
        head.addWidget(self.btn_help)
        lay.addLayout(head)

        # --- settings panel (hidden until asked for: minimal by default)
        self.panel = QFrame()
        self.panel.setObjectName("panel")
        pl = QHBoxLayout(self.panel)
        pl.setContentsMargins(18, 12, 18, 12)
        self.guard_on = QCheckBox("Plane guard")
        self.guard_on.setChecked(self.settings.value("guard", "true") == "true")
        pl.addWidget(self.guard_on)
        pl.addSpacing(16)
        mic_lbl = QLabel("Microphone")
        mic_lbl.setObjectName("muted")
        pl.addWidget(mic_lbl)
        self.mic = QComboBox()
        self._fill_mics()
        pl.addWidget(self.mic, 1)
        self.btn_intro = QPushButton()
        self.btn_intro.clicked.connect(self.choose_intro)
        pl.addWidget(self.btn_intro)
        self.panel.hide()
        lay.addSpacing(10)
        lay.addWidget(self.panel)

        # --- centre stage: status pill · timer · plan · level · companion
        lay.addStretch(2)
        self.light = TrafficLight()
        lay.addWidget(self.light, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(18)
        self.timer = QLabel("00:00:00")
        self.timer.setObjectName("timer")
        self.timer.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self.timer)
        self.plan = QLabel("Press record, or import a take.")
        self.plan.setObjectName("plan")
        self.plan.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self.plan)
        lay.addSpacing(22)
        vu_row = QHBoxLayout()
        vu_row.addStretch(1)
        self.vu = VUMeter()
        self.vu.setFixedWidth(360)
        vu_row.addWidget(self.vu)
        vu_row.addStretch(1)
        lay.addLayout(vu_row)
        # mic problems (clipping, lost buffers): its own line, the status line below is the guard's
        self.mic_warning = QLabel("")
        self.mic_warning.setObjectName("micwarn")
        self.mic_warning.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.mic_warning.setWordWrap(True)
        self.mic_warning.hide()
        self._mic_seen = (0, 0)                 # clipped samples, lost blocks already warned about
        self._mic_warn_until = 0.0
        lay.addWidget(self.mic_warning)
        lay.addSpacing(12)
        self.message = QLabel("")
        self.message.setObjectName("message")
        self.message.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.message.setWordWrap(True)
        lay.addWidget(self.message)
        sky_row = QHBoxLayout()
        sky_row.addStretch(1)
        self.sky = QLabel("")
        self.sky.setObjectName("muted")
        sky_row.addWidget(self.sky)
        self.btn_sky = QPushButton("Sky forecast")
        self.btn_sky.setObjectName("link")
        self.btn_sky.clicked.connect(self.show_sky)
        sky_row.addWidget(self.btn_sky)
        sky_row.addStretch(1)
        lay.addLayout(sky_row)
        lay.addStretch(3)

        # --- bottom: pause · RECORD · finish   (+ import link, companion)
        ctl = QHBoxLayout()
        ctl.setSpacing(28)
        ctl.addStretch(1)
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.setFixedWidth(150)
        self.btn_rec = RecordButton()
        self.btn_rec.setToolTip("Record / stop")
        self.btn_finish = QPushButton("Finish && render")
        self.btn_finish.setObjectName("primary")
        self.btn_finish.setFixedWidth(170)
        ctl.addWidget(self.btn_pause, 0, Qt.AlignmentFlag.AlignVCenter)
        ctl.addWidget(self.btn_rec, 0, Qt.AlignmentFlag.AlignVCenter)
        ctl.addWidget(self.btn_finish, 0, Qt.AlignmentFlag.AlignVCenter)
        ctl.addStretch(1)
        lay.addLayout(ctl)
        lay.addSpacing(10)
        self.btn_import = QPushButton("or import a take")
        self.btn_import.setObjectName("link")
        lay.addWidget(self.btn_import, 0, Qt.AlignmentFlag.AlignHCenter)
        self.btn_listen = QPushButton("Listen back (last 15 s)")
        self.btn_listen.setObjectName("link")
        self.btn_listen.clicked.connect(self.listen_back)
        lay.addWidget(self.btn_listen, 0, Qt.AlignmentFlag.AlignHCenter)
        self.btn_rec.clicked.connect(lambda: self.finish_recording() if self.mode in ("recording", "paused") else self.start_recording())
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_finish.clicked.connect(self.finish_recording)
        self.btn_import.clicked.connect(self.import_take)

        lay.addSpacing(16)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.hide()
        lay.addWidget(self.progress)
        foot = QHBoxLayout()
        self.companion = Companion(ASSETS_DIR)
        foot.addWidget(self.companion, 1)
        lay.addLayout(foot)
        out = QHBoxLayout()
        self.out_label = QLabel("")
        self.out_label.setObjectName("muted")
        self.btn_open = QPushButton("Show video")
        self.btn_open.setObjectName("link")
        self.btn_open.clicked.connect(self.open_output)
        self.btn_open.hide()
        self.btn_upload = QPushButton("Upload to YouTube")
        self.btn_upload.setObjectName("primary")
        self.btn_upload.clicked.connect(self.upload)
        self.btn_upload.hide()
        self.btn_cancel_upload = QPushButton("Pause upload")
        self.btn_cancel_upload.setObjectName("link")
        self.btn_cancel_upload.hide()
        self.btn_yt = QPushButton("Open on YouTube")
        self.btn_yt.setObjectName("link")
        self.btn_yt.hide()
        out.addStretch(1)
        out.addWidget(self.out_label)
        out.addWidget(self.btn_open)
        out.addWidget(self.btn_upload)
        out.addWidget(self.btn_cancel_upload)
        out.addWidget(self.btn_yt)
        out.addStretch(1)
        lay.addLayout(out)

        self.poll = QTimer(self, interval=100, timeout=self._poll_recorder)
        self._update_intro_button()
        self._start_planner()
        self._set_mode("idle")

    # ================================================================== state / look

    def _set_mode(self, mode: str):
        """idle | recording | paused | busy | review | done"""
        self.mode = mode
        rec = mode in ("recording", "paused")
        self.btn_rec.setEnabled(mode in ("idle", "done") or rec)       # while recording it's the stop button
        self.btn_rec.set_recording(rec)
        self.btn_pause.setEnabled(rec)
        self.btn_pause.setText("Resume" if mode == "paused" else "Pause")
        self.btn_finish.setEnabled(rec)
        self.btn_import.setVisible(mode in ("idle", "done"))
        self.btn_listen.setVisible(mode == "paused")
        for w in (self.sky, self.btn_sky):
            w.setVisible(mode in ("idle", "done") and getattr(self, "planner", None) is not None)
        if getattr(self, "planner", None) is not None:
            self.planner.recording = rec          # the recorder feeds the planner while recording
        for w in (self.target, self.mic, self.btn_intro, self.guard_on):
            w.setEnabled(mode in ("idle", "done"))
        if mode == "paused":
            self.light.set_state("paused")
        elif mode in ("idle", "done"):
            self.light.set_state("idle", "Ready" if mode == "idle" else "Video ready")
        elif mode in ("busy", "review"):
            self.light.set_state("idle", "Working…" if mode == "busy" else "Review edits")
        if not rec:
            self._glow("off")

    def _glow(self, light: str, text: str | None = None):
        """Soft window border in the light's colour while recording; none otherwise."""
        if light == "off":
            self.frame.setStyleSheet("QFrame#frame { border: none; }")
            return
        col = theme.LIGHT_COLORS.get(light, theme.MUTED)
        width = 2 if light == "green" else 4
        self.frame.setStyleSheet(f"QFrame#frame {{ border: {width}px solid {col}; }}")
        self.light.set_state(light, text)

    def _target_changed(self):
        self.settings.setValue("target_hours", self.target.currentData())
        if self.edl is not None:
            self._show_plan(self.edl.output_duration())

    def _cfg(self):
        return self.cfg.with_(target_hours=self.target.currentData(), polish=getattr(self, "polish", False))

    def _show_plan(self, session_s: float):
        try:
            head = 30.0 if self.intro_card else 0.0
            p = plan_render(session_s, 0.0, self.target.currentData(), self.cfg.fit_policy, video_head_s=head)
            self.plan.setText(f"→ {p.summary()}")
        except TakeTooShortError:
            self.plan.setText("→ keep going… (too short to loop yet)")

    # ================================================================== quiet-time planner

    def _start_planner(self):
        """Log the sky in the background while the app is open (60 s polls, ~1 km location)."""
        self.planner = None
        if os.environ.get("HUSH_NO_PLANNER") != "1":
            try:
                from core.quiet_planner.service import PlannerService
                self.planner = PlannerService()
                self.planner.start()
            except Exception:
                self.planner = None
        self.sky_timer = QTimer(self, interval=5 * 60 * 1000, timeout=self._refresh_sky)
        self.sky_timer.start()
        QTimer.singleShot(1500, self._refresh_sky)

    def _refresh_sky(self):
        if self.planner is None:
            self.sky.setText("")
            self.btn_sky.hide()
            return
        try:
            from core.quiet_planner.forecast import summary_line
            self.sky.setText(summary_line(self.planner.forecast()))
        except Exception:
            self.sky.setText("")

    def show_sky(self):
        from .sky import SkyDialog
        if self.planner is not None:
            SkyDialog(self.planner.forecast(), parent=self).exec()

    # ================================================================== settings

    def send_debug_report(self):
        from .debug_report import DebugReportDialog
        sessions = sorted((p for p in SESSIONS_DIR.glob("*") if p.is_dir()), reverse=True) if SESSIONS_DIR.exists() else []
        cur = getattr(getattr(self, "session", None), "dir", None)
        cfg = {k: self.settings.value(k) for k in self.settings.allKeys()}
        DebugReportDialog(sessions, cur, cfg, self).exec()

    def _fill_mics(self):
        self.mic.addItem("Default microphone", None)
        try:
            import sounddevice as sd
            seen = set()
            for i, d in enumerate(sd.query_devices()):
                api = sd.query_hostapis(d["hostapi"])["name"]
                if d["max_input_channels"] > 0 and api == "Windows WASAPI" and d["name"] not in seen:
                    seen.add(d["name"])
                    self.mic.addItem(d["name"], i)
        except Exception:
            pass
        saved = self.settings.value("mic")
        if saved is not None:
            k = self.mic.findText(saved)
            if k >= 0:
                self.mic.setCurrentIndex(k)

    def choose_intro(self):
        f, _ = QFileDialog.getOpenFileName(self, "Intro card video", str(PROJECT_ROOT), "Video (*.mp4 *.mov *.mkv)")
        if f:
            self.intro_card = Path(f)
            self.settings.setValue("intro_card", f)
            self._update_intro_button()

    def _update_intro_button(self):
        self.btn_intro.setText(f"Intro card: {self.intro_card.name[:24]}…" if self.intro_card else "Intro card: none")

    # ================================================================== recording

    def _new_session(self) -> Session:
        return Session(self.library.new_dir())

    def show_takes(self):
        from .takes import TakesDialog
        busy = self.mode not in ("idle", "done")
        dlg = TakesDialog(self.library, self, busy=busy)
        if not dlg.exec() or dlg.result_take is None:
            return
        self.reopen_take(dlg.result_take.dir, dlg.result_action)

    def reopen_take(self, folder: Path, action: str = "review"):
        """Load a saved take (clean audio + edit list) back into the editor or straight to render."""
        import soundfile as sf
        from core.edl import EditList
        s = Session(folder)
        try:
            clean, _ = sf.read(s.clean, dtype="float32")
            edl = EditList.load(s.edits)
        except Exception as e:
            self._error(f"Couldn't open that take: {e}")
            return
        self.session = s
        self.output = None
        self.btn_open.hide()
        self.btn_upload.hide()
        self.btn_yt.hide()
        if action == "render":
            self.edl, self.clean = edl, clean
            self._show_plan(edl.output_duration())
            self.render()
        else:
            self._prepared(edl, clean)

    def start_recording(self):
        from core.capture import MicSource, Recorder
        self.settings.setValue("mic", self.mic.currentText())
        self.settings.setValue("guard", "true" if self.guard_on.isChecked() else "false")
        self.session = self._new_session()
        self.edl = self.clean = None
        try:
            self.recorder = Recorder(self.session, source=MicSource(self.mic.currentData()),
                                     guard_enabled=self.guard_on.isChecked(),
                                     use_radar=self.guard_on.isChecked(),
                                     on_radar=self.planner.on_recorder_snapshot if self.planner else None)
            self._reset_mic_warning()
            self.recorder.start()
        except Exception as e:
            self._error(f"Couldn't open the microphone: {e}")
            return
        self.btn_open.hide()
        self.btn_upload.hide()
        self.btn_yt.hide()
        self.out_label.setText(f"Recording to {self.session.dir.name} · noise removal: {self.recorder.status.denoise_backend}")
        self.companion.say("recording")
        self._set_mode("recording")
        self.poll.start()

    def toggle_pause(self):
        if self.recorder is None:
            return
        if self.mode == "recording":
            self.recorder.pause()
            self.companion.say("paused")
            self._set_mode("paused")
        else:
            self.recorder.resume()
            self.companion.say("recording")
            self._set_mode("recording")

    def listen_back(self):
        if self.recorder is None:
            return
        try:
            import sounddevice as sd
            sd.stop()
            sd.play(self.recorder.listen_back(15.0), SR)
        except Exception as e:
            self.message.setText(f"Couldn't play: {e}")

    def _poll_recorder(self):
        r = self.recorder
        if r is None:
            return
        s = r.status
        self.timer.setText(_hms(s.take_s))
        self.vu.set_levels(s.level_db, s.peak_db, clipped=self._check_mic(s))
        self._show_plan(max(s.take_s, 1.0))
        if self.mode == "paused":
            return
        pill = f"Plane in ~{s.eta_s:.0f}s, finish your sentence" if s.light == "amber" and s.eta_s else None
        self._glow(s.light, pill)
        mood = {"green": "recording", "amber": "amber", "red": "red", "clear": "clear"}.get(s.light, "recording")
        # the companion speaks the light; the status line carries only the details (radar ETA etc.)
        detail = s.message.split("(", 1)[1].rstrip(")") if "(" in s.message else ""
        self.message.setText(detail)
        if self.companion.mood != mood or mood == "amber":
            self.companion.say(mood, s.message.split(" (")[0] if mood == "amber" else None)
        if s.chime:
            s.chime = False
            chime((660.0, 880.0) if s.light == "clear" else (520.0,))

    def _check_mic(self, s) -> bool:
        """Clipping or lost input buffers since the last poll → warning under the meter.
        Returns whether the input clipped (the peak counts too, so it shows while paused)."""
        now = time.monotonic()
        clipped = s.clipped_samples > self._mic_seen[0] or s.peak_db >= CLIP_DB
        dropped = s.overflows + s.dropped_blocks > self._mic_seen[1]
        self._mic_seen = (s.clipped_samples, s.overflows + s.dropped_blocks)
        if clipped:
            self.mic_warning.setText(MIC_CLIP_WARNING)
            self._mic_warn_until = now + MIC_WARN_HOLD_S
        elif dropped:
            self.mic_warning.setText(MIC_DROP_WARNING)
            self._mic_warn_until = now + MIC_WARN_HOLD_S
        self.mic_warning.setVisible(now < self._mic_warn_until)
        return clipped

    def _reset_mic_warning(self):
        self._mic_seen = (0, 0)
        self._mic_warn_until = 0.0
        self.mic_warning.hide()
        self.vu.reset()

    def finish_recording(self):
        if self.recorder is None:
            return
        self.poll.stop()
        self._set_mode("busy")
        self.companion.say("preparing")
        self.message.setText("Finishing the last bit of noise removal…")
        rec = self.recorder
        self.recorder = None
        self._run(FuncWorker(rec.stop), self._recorded)

    def _recorded(self, result):
        w = PrepareWorker(self.session, recording=result, cfg=self._cfg())
        w.stage.connect(self.message.setText)
        self._run(w, self._prepared)

    # ================================================================== import

    def import_take(self):
        f, _ = QFileDialog.getOpenFileName(self, "Import a take (raw Riverside export, wav, mp3…)",
                                           str(PROJECT_ROOT), "Audio (*.wav *.flac *.mp3 *.m4a *.aac)")
        if not f:
            return
        self.import_file(Path(f))

    def import_file(self, path: Path):
        self.session = self._new_session()
        self._set_mode("busy")
        self.companion.say("preparing")
        self.btn_open.hide()
        w = PrepareWorker(self.session, import_src=path, cfg=self._cfg())
        w.stage.connect(self.message.setText)
        self._run(w, self._prepared)

    # ================================================================== review + render

    def _capture_report(self) -> dict | None:
        """capture.json of a live take (T7/T8); None for imports."""
        try:
            return json.loads((self.session.dir / "capture.json").read_text("utf-8"))
        except (OSError, ValueError, AttributeError):
            return None

    def _prepared(self, edl, clean, quality=None):
        self.edl, self.clean = edl, clean
        self._show_plan(edl.output_duration())
        self.timer.setText(_hms(edl.output_duration()))
        self._set_mode("review")
        self.companion.say("review")
        dlg = ReviewDialog(edl, clean, self, capture=self._capture_report(), quality=quality)
        if dlg.exec():
            self.polish = dlg.polish.isChecked()
            self.render()
        else:
            self._set_mode("done")
            self.message.setText("Edits saved. Import or record again, or reopen this session later.")
            edl.save(self.session.edits)

    def render(self):
        cfg = self._cfg()
        self._set_mode("busy")
        self.companion.say("rendering", n="…", h=cfg.target_hours)
        self.progress.setValue(0)
        self.progress.show()
        w = RenderWorker(self.session, cfg, self.assets, self.intro_card, self.clean, self.edl)
        w.progress.connect(lambda f: self.progress.setValue(int(f * 1000)))
        self._run(w, self._rendered)

    def _rendered(self, plan, out, rep, tm):
        self.progress.hide()
        self.output = Path(out)
        self._set_mode("done")
        self.companion.say("success")
        total = sum(tm.steps.values())
        self.message.setText(f"{plan.summary()} · {rep.output_lufs:.1f} LUFS · rendered in {total:.0f}s")
        self.out_label.setText(self.output.name)
        self.btn_open.show()
        self.btn_upload.show()
        self.btn_yt.hide()
        self.rendered_hours = plan.target_s / 3600

    # ================================================================== YouTube

    def upload(self):
        from .publish import PublishDialog, UploadWorker
        if not self.output or not self.output.exists():
            return
        dlg = PublishDialog(self.output, getattr(self, "rendered_hours", self._cfg().target_hours), self)
        if not dlg.exec() or dlg.meta is None:
            return
        self.btn_upload.hide()
        self.progress.setValue(0)
        self.progress.show()
        self.companion.say("rendering", text="Sending to YouTube… 0%")
        self.light.set_state("idle", "Uploading…")
        w = UploadWorker(self.output, dlg.meta)
        w.progress.connect(self._upload_progress)
        self.btn_cancel_upload.clicked.connect(w.cancel.set)
        self.btn_cancel_upload.show()
        self._workers.append(w)
        w.done.connect(self._uploaded)
        w.failed.connect(self._upload_failed)
        w.finished.connect(lambda: (self.btn_cancel_upload.hide(), self._workers.remove(w) if w in self._workers else None))
        w.start()

    def _upload_progress(self, f: float):
        self.progress.setValue(int(f * 1000))
        self.companion.say("rendering", text=f"Sending to YouTube… {f * 100:.0f}%")

    def _uploaded(self, res):
        from core.youtube_publisher import UNVERIFIED_NOTE
        self.progress.hide()
        self.light.set_state("idle", "Uploaded")
        self.companion.say("success", text="Uploaded! Tap to open.")
        self.message.setText(UNVERIFIED_NOTE if res.locked_private else f"Uploaded as {res.privacy}.")
        self.btn_yt.show()
        try:
            self.library.mark_uploaded(self.session.dir, res.url)
        except Exception:
            pass
        try:
            self.btn_yt.clicked.disconnect()
        except TypeError:
            pass
        self.btn_yt.clicked.connect(lambda: __import__("webbrowser").open(res.url))

    def _upload_failed(self, msg: str):
        self.progress.hide()
        self.btn_upload.show()
        self.btn_upload.setText("Resume upload" if "paused" in msg.lower() or "retries" in msg.lower() else "Upload to YouTube")
        self.light.set_state("idle", "Upload stopped")
        self.companion.say("error", text=msg.strip().splitlines()[-1][:140])

    def open_output(self):
        if self.output and self.output.exists():
            if sys.platform == "win32":
                subprocess.run(["explorer", "/select,", str(self.output)])
            else:
                subprocess.run(["xdg-open", str(self.output.parent)])

    # ================================================================== helpers

    def _run(self, worker: QThread, on_done):
        self._workers.append(worker)
        worker.done.connect(on_done)
        worker.failed.connect(self._error)
        worker.finished.connect(lambda: self._workers.remove(worker) if worker in self._workers else None)
        worker.start()

    def _error(self, details: str):
        self.progress.hide()
        self._set_mode("done")
        self.companion.say("error")
        self.message.setText(details.strip().splitlines()[-1][:200])
        box = QMessageBox(self)
        box.setWindowTitle("Something went wrong")
        box.setText(details.strip().splitlines()[-1][:300])
        box.setDetailedText(details)
        box.exec()

    def closeEvent(self, e):
        if getattr(self, "planner", None) is not None:
            self.planner.stop()
        if self.recorder is not None:
            try:
                self.recorder.stop()
            except Exception:
                pass
        super().closeEvent(e)


def _hms(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
