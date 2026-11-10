"""Background workers (QThread). Widgets are never touched off the GUI thread: workers only
emit signals, which Qt delivers to the GUI thread (queued connections)."""
from __future__ import annotations

import time
import traceback
from pathlib import Path

import numpy as np
import soundfile as sf
from PyQt6.QtCore import QThread, pyqtSignal

from core.asset_cache import AssetCache
from core.audio_quality import analyze
from core.config import RenderConfig
from core.edl import EditList
from core.session import Session, denoise, finish, import_take, prepare


class PrepareWorker(QThread):
    """Imported file: import → noise removal → edits. Recorded take: edits only (the live ear
    already heard the planes and noise removal ran during the take)."""
    stage = pyqtSignal(str)
    done = pyqtSignal(object, object, object)     # EditList, clean ndarray, QualityReport
    failed = pyqtSignal(str)

    def __init__(self, session: Session, import_src: Path | None = None, recording=None,
                 cfg: RenderConfig | None = None, parent=None):
        super().__init__(parent)
        self.session, self.import_src, self.recording = session, import_src, recording
        self.cfg = cfg or RenderConfig()

    def run(self):
        try:
            t0 = time.perf_counter()
            if self.import_src is not None:
                self.stage.emit("Importing the take…")
                raw = import_take(self.import_src, self.session)
                self.stage.emit("Removing background noise…")
                clean, backend = denoise(self.session, raw)
                self.stage.emit("Listening for planes and finding pauses…")
                edl = prepare(self.session, raw, clean, render_cfg=self.cfg)
            else:
                rec = self.recording
                clean = rec.clean
                self.stage.emit("Finding pauses…")
                edl = prepare(self.session, None, clean, ear_events=rec.ear_events,
                              render_cfg=self.cfg)
            source, _ = sf.read(self.session.raw, dtype="float32", always_2d=False)
            if source.ndim > 1:
                source = source.mean(axis=1)
            quality = analyze(source, clean)
            self.stage.emit(f"Ready to review ({time.perf_counter() - t0:.0f}s)")
            self.done.emit(edl, clean, quality)
        except Exception:
            self.failed.emit(traceback.format_exc())


class RenderWorker(QThread):
    progress = pyqtSignal(float)
    stage = pyqtSignal(str)
    done = pyqtSignal(object, object, object, object)   # plan, out path, master report, timings
    failed = pyqtSignal(str)

    def __init__(self, session: Session, cfg: RenderConfig, assets: AssetCache, intro_card: Path | None,
                 clean: np.ndarray, edl: EditList, parent=None):
        super().__init__(parent)
        self.session, self.cfg, self.assets, self.intro_card = session, cfg, assets, intro_card
        self.clean, self.edl = clean, edl

    def run(self):
        try:
            self.edl.save(self.session.edits)
            self.stage.emit("mastering")
            plan, out, rep, tm = finish(self.session, self.cfg, self.assets, self.intro_card, clean=self.clean,
                                        edl=self.edl, on_progress=self.progress.emit)
            self.done.emit(plan, out, rep, tm)
        except Exception:
            self.failed.emit(traceback.format_exc())
