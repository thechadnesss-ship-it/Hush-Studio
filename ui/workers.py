"""Background workers (QThread). Widgets are never touched off the GUI thread: workers only
emit signals, which Qt delivers to the GUI thread (queued connections)."""
from __future__ import annotations

import time
import traceback
import logging
from pathlib import Path

import numpy as np
import soundfile as sf
from PyQt6.QtCore import QThread, pyqtSignal

from core.asset_cache import AssetCache
from core.audio_quality import analyze
from core.config import RenderConfig
from core.edl import RAW_SOURCE, EditList
from core.session import SR, Session, denoise, finish, import_take, prepare

logger = logging.getLogger(__name__)


class ImportWorker(QThread):
    """Imported file → raw.flac (48 kHz mono) and an empty edit list on it. Nothing else runs:
    Sam chooses the processing steps in the editor (T26)."""
    stage = pyqtSignal(str)
    done = pyqtSignal(object, object)             # EditList (source raw.flac), raw ndarray
    failed = pyqtSignal(str)

    def __init__(self, session: Session, src: Path, parent=None):
        super().__init__(parent)
        self.session, self.src = session, src

    def run(self):
        try:
            t0 = time.perf_counter()
            self.stage.emit("Importing the take…")
            raw = import_take(self.src, self.session)
            edl = EditList(duration=len(raw) / SR, sample_rate=SR, source=RAW_SOURCE)
            edl.save(self.session.edits)
            logger.info("imported %s: %.1fs audio in %.2fs", self.src.name, len(raw) / SR, time.perf_counter() - t0)
            self.done.emit(edl, raw)
        except Exception:
            self.failed.emit(traceback.format_exc())


class PrepareWorker(QThread):
    """Recorded take: edits only (the live ear already heard the planes and noise removal ran
    during the take). Imported file: import → noise removal → edits, all at once; the app now
    imports with ImportWorker instead and Sam picks the steps (T26), this path stays for scripts."""
    stage = pyqtSignal(str)
    done = pyqtSignal(object, object, object)     # EditList, clean ndarray, raw source (or None)
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
                raw = None
                clean = rec.clean
                self.stage.emit("Finding pauses…")
                edl = prepare(self.session, None, clean, ear_events=rec.ear_events,
                              render_cfg=self.cfg)
            self.stage.emit(f"Ready to review ({time.perf_counter() - t0:.0f}s)")
            # The potentially long source-vs-clean check belongs to the review worker,
            # not on the speed-critical prepare path.  Keep the source as an ndarray for
            # imports and a path for recordings so ReviewDialog can load it off-thread.
            # Audacity round-trips are a new source timeline; comparing raw.flac
            # against them would manufacture gaps and distortion warnings.
            source = (raw if raw is not None else self.session.raw) if edl.source == "clean.wav" else None
            self.done.emit(edl, clean, source)
        except Exception:
            self.failed.emit(traceback.format_exc())


class AudioQualityWorker(QThread):
    """Run the optional source-vs-clean check away from the Qt GUI thread."""
    done = pyqtSignal(object)

    def __init__(self, source: np.ndarray | Path, processed: np.ndarray, parent=None):
        super().__init__(parent)
        self.source, self.processed = source, processed

    def run(self):
        try:
            source = self.source
            if isinstance(source, Path):
                source, _ = sf.read(source, dtype="float32", always_2d=False)
                if source.ndim > 1:
                    source = source.mean(axis=1)
            # Mastering re-gains and limits later, so pre-mastering true-peak
            # headroom is informational here, not a reason to reject a take.
            self.done.emit(analyze(source, self.processed, warn_true_peak=False))
        except Exception:
            logger.exception("Audio quality check failed")
            self.done.emit(None)


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
