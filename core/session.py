"""One session, end to end: raw take → reviewable edits → mastered session.m4a → long video.

    sessions/<ts>/raw.flac        untouched capture (or an imported take, resampled to 48 kHz mono)
    sessions/<ts>/clean.wav       background noise removed
    sessions/<ts>/edits.json      EVERY change (plane cuts, pause shaping, Sam's tweaks), reviewable
    sessions/<ts>/session.m4a     edits applied + mastered + encoded ONCE
    sessions/<ts>/output_<h>h.mp4 intro card + black, voice looped to the exact target length

`prepare()` builds the automatic edit list; the UI shows it for review; `finish()` renders.
Nothing is destructive: re-running `finish()` after Sam changes edits.json is cheap.
"""
from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

from . import ffmpeg_tools as ff
from . import renderer
from .asset_cache import AssetCache
from .auto_edit import AutoEditConfig, auto_edit, find_flubs, find_markers
from .config import RenderConfig
from .denoise import Denoiser, SpectralGate, deepfilter_array, deepfilter_exe, denoise_array
from .edl import Cut, EditList
from .master import MasterReport, master_to_m4a
from .plane_guard.ear import Ear, EarEvent, holds_from_events
from .plane_guard.splicer import add_plane_cuts
from .vad import detect_speech

SR = 48000


@dataclass
class Session:
    dir: Path

    @property
    def raw(self) -> Path: return self.dir / "raw.flac"
    @property
    def clean(self) -> Path: return self.dir / "clean.wav"
    @property
    def edits(self) -> Path: return self.dir / "edits.json"
    @property
    def m4a(self) -> Path: return self.dir / "session.m4a"

    def output(self, hours: float) -> Path:
        return self.dir / f"output_{hours:g}h.mp4"


@dataclass
class Timings:
    steps: dict[str, float] = field(default_factory=dict)

    def mark(self, name: str, t0: float) -> float:
        now = time.perf_counter()
        self.steps[name] = round(now - t0, 2)
        return now


def load_mono_48k(path: Path) -> np.ndarray:
    """Any audio file → float32 mono 48 kHz (Riverside exports are 44.1 kHz)."""
    r = subprocess.run([ff.ffmpeg_bin(), "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR),
                        "-f", "f32le", "-"], capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.float32).copy()


def import_take(src: Path, session: Session) -> np.ndarray:
    session.dir.mkdir(parents=True, exist_ok=True)
    x = load_mono_48k(src)
    sf.write(session.raw, x, SR, format="FLAC", subtype="PCM_24")
    return x


def denoise(session: Session, x: np.ndarray, denoiser: Denoiser | None = None,
            attenuation_limit_db: float | None = None) -> tuple[np.ndarray, str]:
    """DeepFilterNet if installed (all cores, split at pauses), else the built-in spectral gate.
    Writes clean.wav. Pass `denoiser` to force a specific streaming backend."""
    if attenuation_limit_db is None:
        attenuation_limit_db = 100.0 if denoiser is None and deepfilter_exe() is not None else 18.0
    from .log_setup import get_logger
    log = get_logger("denoise")
    log.info("denoise start: %.1fs audio, attenuation limit %s dB", len(x) / SR, attenuation_limit_db)
    if denoiser is None and deepfilter_exe() is not None:
        y = deepfilter_array(x, SR, attenuation_limit_db, work_dir=session.dir)
        sf.write(session.clean, y, SR, subtype="FLOAT")
        log.info("denoise done: deepfilternet")
        return y, "deepfilternet"
    from .denoise import SpectralGateConfig
    d = denoiser or SpectralGate(SpectralGateConfig(attenuation_limit_db=attenuation_limit_db))
    y = denoise_array(x, d, chunk=SR * 60)       # whole files: big batches (live REC uses 100 ms)
    sf.write(session.clean, y, SR, subtype="FLOAT")
    log.info("denoise done: %s", type(d).__name__)
    return y, type(d).__name__


def prepare(session: Session, raw: np.ndarray | None, clean: np.ndarray,
            ear_events: list[EarEvent] | None = None, auto: AutoEditConfig | None = None,
            marker_template: np.ndarray | None = None,
            render_cfg: RenderConfig | None = None) -> EditList:
    """Automatic edit list. `ear_events` come from the live guard during REC; for an imported
    take the ear is run over the raw audio now (same detector, offline)."""
    if auto is None:
        auto = (render_cfg or RenderConfig()).auto_edit_config()
    if ear_events is None:
        if raw is None:
            raise ValueError("need the raw take (or the live ear events) to find planes")
        ear = Ear(SR)
        ear.feed(raw)
        ear_events = ear.events
    dur = len(clean) / SR
    speech = detect_speech(clean, SR)
    edl = EditList(duration=dur, sample_rate=SR)
    holds = holds_from_events(ear_events, dur)
    add_plane_cuts(edl, holds, speech, onsets=[e.t for e in ear_events if e.kind == "rising"])
    edl.pauses, template = auto_edit(clean, SR, speech, dur, auto, cuts=edl.active_cuts(), template=marker_template)
    if auto.flub_detect:
        markers, _ = find_markers(clean, SR, speech, auto, template) if auto.marker_detect else ([], None)
        for s, e in find_flubs(clean, SR, speech, markers, auto):
            if not any(s < c.end and c.start < e for c in edl.cuts):          # plane cuts win
                edl.cuts.append(Cut(s, e, "flub", keep=True, proposed=True, note="said again"))
    edl.save(session.edits)
    return edl


def finish(session: Session, cfg: RenderConfig, assets: AssetCache, intro_card: Path | None,
           clean: np.ndarray | None = None, edl: EditList | None = None,
           on_progress=None, cancel: threading.Event | None = None) -> tuple[renderer.RenderPlan, Path, MasterReport, Timings]:
    """Apply edits → master → encode once → stitch the long video. Returns timings per step."""
    tm = Timings()
    t = time.perf_counter()
    if clean is None:
        clean, _ = sf.read(session.clean, dtype="float32")
    edl = edl or EditList.load(session.edits)
    edited = edl.render(clean)
    t = tm.mark("apply_edits", t)
    rep = master_to_m4a(edited, SR, session.m4a, cfg)
    t = tm.mark("master_encode", t)
    card = assets.intro_video(intro_card) if intro_card else None
    black = assets.black_loop()
    t = tm.mark("assets", t)
    out = session.output(cfg.target_hours)
    plan, _ = renderer.render(cfg, session=session.m4a, black_loop=black, intro_video=card, out_path=out,
                              silence=assets.silence(), on_progress=on_progress, cancel=cancel)
    tm.mark("render", t)
    return plan, out, rep, tm
