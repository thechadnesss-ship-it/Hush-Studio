"""Live recording (CLAUDE.md Module B), with the plane guard running live.

    mic callback ──put_nowait──► queue ──► worker thread:
                                           • calibrated input gain (T27), then
                                           • raw.flac (always: the unedited backup)
                                           • ear + guard → traffic light (take time)
                                           • VU level
                                           • background noise removal on finished segments
    radar thread ── every 5 s ──► guard.on_radar()

The audio callback ONLY copies and enqueues: no DSP, no file I/O (PortAudio rule).
PAUSE: audio is discarded while paused, so take time = recorded audio, not wall clock.
Plane holds never stop capture; they become cuts in the edit list at FINISH.

Background noise removal: with DeepFilterNet installed, every ~60 s of take (split inside a
pause) is sent to a worker; at STOP only the last segment is left. Otherwise the streaming
SpectralGate runs inline (≈ 60× real time).
"""
from __future__ import annotations

import collections
import concurrent.futures as cf
import json
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import numpy as np
import soundfile as sf

from .log_setup import get_logger

log = get_logger("capture")

from .calibration import LevelWatch
from .denoise import DFN_PREROLL_S, SpectralGate, deepfilter_array, deepfilter_exe, deepfilter_input_gain_db
from .plane_guard.ear import Ear
from .plane_guard.guard import Guard, GuardUpdate
from .plane_guard.sky_radar import Home, SkyRadar
from .session import Session

SR = 48000
BLOCK = 4800                     # 100 ms
MAX_LOST_SPOTS = 200
CLIP_LEVEL = 0.999               # |sample| at or above this counts as clipped (16-bit max is 0.99997)


# =========================================================================== sources

class AudioSource(Protocol):
    def start(self, on_block: Callable[[np.ndarray], None]) -> None: ...
    def stop(self) -> None: ...


class MicSource:
    """sounddevice input at 48 kHz mono float32. `device` = index or name substring (None = default)."""

    def __init__(self, device: int | str | None = None):
        self.device = device
        self.stream = None
        self.overflows = 0              # PortAudio input overflows: samples lost → clicks
        self.info: dict = {}

    def start(self, on_block):
        import sounddevice as sd

        def cb(indata, frames, t, status):
            if status.input_overflow:
                self.overflows += 1
            on_block(indata[:, 0].copy())          # copy + enqueue only

        from .mic_select import candidates
        tries: list = []
        try:
            tries = [(c.device, sd.WasapiSettings(exclusive=True) if c.exclusive else None, c.label)
                     for c in candidates(list(sd.query_devices()), list(sd.query_hostapis()), self.device)]
        except Exception:
            log.exception("mic selection failed; using the system default")
        tries.append((self.device, None, "system default"))      # last resort: the old behaviour
        mode, err = "", None
        for dev, extra, label in tries:
            try:
                self.stream = sd.InputStream(device=dev, samplerate=SR, channels=1, dtype="float32",
                                             blocksize=BLOCK, callback=cb, extra_settings=extra)
                self.stream.start()
                mode = label
                break
            except Exception as e:
                err = e
                self.stream = None
                log.info("could not open %s (%s): %s", dev, label, e)
        if self.stream is None:
            raise err
        try:
            d = sd.query_devices(self.stream.device)
            self.info = {"device": d["name"], "host_api": sd.query_hostapis(d["hostapi"])["name"], "mode": mode,
                         "device_default_rate": d["default_samplerate"], "stream_rate": self.stream.samplerate}
            log.info("input opened: %s [%s] %s Hz", self.info["device"], self.info["host_api"], self.info["stream_rate"])
        except Exception:
            self.info = {}

    def stop(self):
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None


class ArraySource:
    """Feeds an array as if it were a mic (tests, demos). `realtime=False` = as fast as possible."""

    def __init__(self, x: np.ndarray, realtime: bool = False):
        self.x = np.asarray(x, dtype=np.float32)
        self.realtime = realtime
        self.blocking = not realtime          # faster than real time → wait for the recorder
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self.done = threading.Event()

    def start(self, on_block):
        def run():
            t0 = time.perf_counter()
            for i in range(0, len(self.x), BLOCK):
                if self._stop.is_set():
                    break
                on_block(self.x[i:i + BLOCK].copy())
                if self.realtime:
                    time.sleep(max(0.0, t0 + (i + BLOCK) / SR - time.perf_counter()))
            self.done.set()
        self._t = threading.Thread(target=run, daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join()


# =========================================================================== recorder

@dataclass
class LiveStatus:
    take_s: float = 0.0
    level_db: float = -120.0         # VU: RMS of the last block
    peak_db: float = -120.0
    paused: bool = False
    light: str = "green"
    message: str = "Ready to record your session."
    eta_s: float | None = None
    chime: bool = False
    dropped_blocks: int = 0
    denoise_backend: str = ""
    clipped_samples: int = 0         # at/near full scale: the mic input is too loud (harsh, clipped)
    over_full_scale: int = 0         # float input beyond ±1.0 (WASAPI); raw.flac stores these clipped
    overflows: int = 0               # input buffers lost by the sound driver (crackle)
    last_clip_s: float | None = None # take time of the latest clipped block
    lost_at_s: list = field(default_factory=list)   # take times where sound was lost (T8)
    input_gain_db: float = 0.0       # calibrated gain applied to every block before raw.flac (T27)
    level_advice: str = ""           # "" | "quiet" | "loud": her voice is out of range (T27)


@dataclass
class RecordingResult:
    raw_path: Path
    clean: np.ndarray
    ear_events: list
    duration_s: float
    backend: str
    guard_history: list[GuardUpdate] = field(default_factory=list)


class Recorder:
    def __init__(self, session: Session, source: AudioSource | None = None, home: Home = Home(),
                 radar: SkyRadar | None = None, use_radar: bool = True, guard_enabled: bool = True,
                 segment_s: float = 60.0, on_status: Callable[[LiveStatus], None] | None = None,
                 on_radar: Callable | None = None, input_gain_db: float = 0.0,
                 floor_db: float | None = None):
        from .plane_guard.guard import GuardConfig
        self.session = session
        self.source = source or MicSource()
        self.ear = Ear(SR)
        self.guard = Guard(self.ear, cfg=GuardConfig(enabled=guard_enabled))
        self.radar = radar if radar is not None else (SkyRadar(home) if use_radar else None)
        self.segment_s = segment_s
        self.on_status = on_status
        self.on_radar = on_radar             # e.g. the quiet-time planner's logger
        self.status = LiveStatus(input_gain_db=float(input_gain_db))
        # T27: the mic's calibrated gain, applied before anything else so raw.flac, the ear, noise
        # removal and the meter all see the take at a healthy level (capture.json keeps the gain).
        self._gain = float(10 ** (input_gain_db / 20))
        # the room floor after gain: calibrated floor (measured without gain) + gain
        self.level_watch = LevelWatch(None if floor_db is None else floor_db + input_gain_db)
        self.q: queue.Queue[np.ndarray] = queue.Queue(maxsize=600)   # 60 s of slack
        self._lock = threading.Lock()                                # guard is touched by 2 threads
        self._paused = False
        self._running = threading.Event()
        self._worker: threading.Thread | None = None
        self._radar_t: threading.Thread | None = None
        self._samples = 0
        # where sound was lost, in callback order: (blocks accepted so far) at the moment of loss.
        # The worker resolves each to a take time once it has consumed that many blocks (T8), so the
        # spot stays right when the queue is delayed or full.
        self._accepted = 0
        self._consumed = 0
        self._loss_marks: collections.deque[int] = collections.deque()
        self._seen_overflows = 0
        # noise removal
        self.use_dfn = deepfilter_exe() is not None
        self.status.denoise_backend = "DeepFilterNet" if self.use_dfn else "SpectralGate"
        self._gate = None if self.use_dfn else SpectralGate()
        self._clean_parts: list[np.ndarray] = []
        self._pending: list[np.ndarray] = []        # raw blocks of the current DFN segment
        self._pending_len = 0
        self._pending_db: list[float] = []
        self._dfn_context = np.zeros(0, np.float32)     # end of the previous segment: model pre-roll
        self._dfn_stage_gain_db: float | None = None    # one gain for the whole live take
        self._futures: list[cf.Future] = []
        self._pool = cf.ThreadPoolExecutor(max_workers=2) if self.use_dfn else None
        self._raw_file: sf.SoundFile | None = None
        self._recent = collections.deque(maxlen=int(30 * SR / BLOCK))   # last 30 s for "listen back"
        self._peak_db = -120.0

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        self.session.dir.mkdir(parents=True, exist_ok=True)
        log.info("recording started: %s", self.session.dir.name)
        self._raw_file = sf.SoundFile(self.session.raw, "w", SR, 1, format="FLAC", subtype="PCM_24")
        self._running.set()
        self._worker = threading.Thread(target=self._work, name="recorder", daemon=True)
        self._worker.start()
        if self.radar is not None:
            self._radar_t = threading.Thread(target=self._radar_loop, name="radar", daemon=True)
            self._radar_t.start()
        # a real mic must never block (drop + count instead); a simulated source running faster
        # than real time asks for backpressure instead
        self.source.start(self.q.put if getattr(self.source, "blocking", False) else self._enqueue)

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def listen_back(self, seconds: float = 15.0) -> np.ndarray:
        """The last `seconds` of the take, background noise removed (for headphones, while paused).
        A live monitor would reach her ears ~150 ms late, which makes speaking hard; this doesn't."""
        blocks = list(self._recent)
        if not blocks:
            return np.zeros(0, np.float32)
        x = np.concatenate(blocks)[-int(seconds * SR):]
        from .denoise import denoise_array
        return denoise_array(x, SpectralGate(), chunk=len(x))

    @property
    def take_s(self) -> float:
        return self._samples / SR

    def stop(self) -> RecordingResult:
        self.source.stop()
        self._running.clear()
        if self._worker:
            self._worker.join()
        self._raw_file.close()
        # finish noise removal
        if self.use_dfn:
            self._submit_segment()
            for f in self._futures:
                f.result()
            self._pool.shutdown()
            clean = np.concatenate([f.result() for f in self._futures]) if self._futures else np.zeros(0, np.float32)
        else:
            tail = self._gate.flush()
            clean = np.concatenate(self._clean_parts + [tail])[self._gate.latency_samples:]
        clean = np.concatenate([clean, np.zeros(max(0, self._samples - len(clean)), np.float32)])[: self._samples]
        sf.write(self.session.clean, clean, SR, subtype="FLOAT")
        self._write_capture_report()
        rep = self.capture_report()
        log.info("recording stopped: take %.1fs, peak %.1f dBFS, clipped %s, lost buffers %s, denoise %s",
                 rep["take_s"], rep["peak_dbfs"], rep["clipped_samples"], rep["lost_buffers"], rep["denoise_backend"])
        return RecordingResult(self.session.raw, clean, list(self.ear.events), self.take_s,
                               self.status.denoise_backend, list(self.guard.history))

    def capture_report(self) -> dict:
        """What the input actually delivered (T7): device, clipping, lost buffers. Saved as
        capture.json next to raw.flac, so a take recorded elsewhere can be diagnosed from it."""
        s = self.status
        return {"take_s": round(self.take_s, 2), "denoise_backend": s.denoise_backend,
                **getattr(self.source, "info", {}),
                "input_gain_db": s.input_gain_db,
                "peak_dbfs": round(self._peak_db, 2), "clipped_samples": s.clipped_samples,
                "over_full_scale_samples": s.over_full_scale,
                "input_overflows": getattr(self.source, "overflows", 0), "dropped_blocks": s.dropped_blocks,
                "lost_buffers": getattr(self.source, "overflows", 0) + s.dropped_blocks,
                "lost_at_s": [round(t, 2) for t in s.lost_at_s]}

    def _write_capture_report(self) -> None:
        try:
            (self.session.dir / "capture.json").write_text(json.dumps(self.capture_report(), indent=2), "utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------ threads

    def _enqueue(self, block: np.ndarray) -> None:      # runs in the audio callback
        overflows = getattr(self.source, "overflows", 0)
        if overflows > self._seen_overflows:            # the driver lost sound just before this block
            for _ in range(overflows - self._seen_overflows):   # one mark per lost buffer
                self._loss_marks.append(self._accepted)
            self._seen_overflows = overflows
        try:
            self.q.put_nowait(block)
            self._accepted += 1
        except queue.Full:
            self.status.dropped_blocks += 1
            self._loss_marks.append(self._accepted)     # the dropped block belongs right here

    def _work(self) -> None:
        while self._running.is_set() or not self.q.empty():
            try:
                b = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            if self._gain != 1.0:
                b = (b * self._gain).astype(np.float32)
            rms = float(np.sqrt(np.mean(b.astype(np.float64) ** 2)) + 1e-12)
            self.status.level_db = 20 * np.log10(rms)
            self.status.peak_db = float(20 * np.log10(np.abs(b).max(initial=1e-12)))
            self.status.paused = self._paused
            self._consumed += 1
            if self._paused:
                self._resolve_losses(record=False)
                self._publish(None)
                continue
            self._raw_file.write(b)
            self._recent.append(b)
            self._samples += len(b)
            self._check_input(b)
            self.status.level_advice = self.level_watch.feed(self.status.level_db, self.status.peak_db)
            self._resolve_losses()
            with self._lock:
                self.ear.feed(b)
                upd = self.guard.tick(self.take_s)
            self._denoise(b)
            self._publish(upd)

    def _check_input(self, b: np.ndarray) -> None:
        """`b` is the block after the calibrated gain. Clipping counts at the mic (before a
        reducing gain: the mic itself is too hot) and in what is recorded (after a boost)."""
        a = np.abs(b)
        if self._gain < 1.0:
            a = a / self._gain                      # back to the mic's own level
        self._peak_db = max(self._peak_db, self.status.peak_db)
        clipped = int(np.count_nonzero(a >= CLIP_LEVEL))
        if clipped:
            self.status.clipped_samples += clipped
            self.status.over_full_scale += int(np.count_nonzero(a > 1.0))
            self.status.last_clip_s = self.take_s
        self.status.overflows = getattr(self.source, "overflows", 0)

    def _resolve_losses(self, record: bool = True) -> None:
        """Turn loss marks whose position has been reached into take times (worker thread)."""
        while self._loss_marks and self._loss_marks[0] <= self._consumed:
            self._loss_marks.popleft()
            if record:
                self._mark_lost(self.take_s)

    def _mark_lost(self, t: float) -> None:
        """Remember where in the take sound went missing (once per 0.5 s, at most 200 spots), so
        review can play each one. The total is kept separately (lost_buffers)."""
        lost = self.status.lost_at_s
        if len(lost) < MAX_LOST_SPOTS and (not lost or t - lost[-1] >= 0.5):
            lost.append(t)

    def _publish(self, upd: GuardUpdate | None) -> None:
        s = self.status
        s.take_s = self.take_s
        if upd is not None:
            s.light, s.message, s.eta_s, s.chime = upd.light.value, upd.message, upd.eta_s, upd.chime
        else:
            s.chime = False
        if self.on_status:
            self.on_status(s)

    def _radar_loop(self) -> None:
        while self._running.is_set():
            try:
                snap = self.radar.poll()
                with self._lock:
                    self.guard.on_radar(snap, self.take_s)
                if self.on_radar:
                    self.on_radar(snap)
            except Exception:
                pass                                       # radar is best-effort; the ear still works
            for _ in range(int(self.radar.cfg.poll_recording_s * 10)):
                if not self._running.is_set():
                    break
                time.sleep(0.1)

    # ------------------------------------------------------------------ noise removal

    def _denoise(self, b: np.ndarray) -> None:
        if not self.use_dfn:
            self._clean_parts.append(self._gate.process(b))
            return
        self._pending.append(b)
        self._pending_len += len(b)
        self._pending_db.append(self.status.level_db)
        # cut a segment once it's long enough AND the last 0.5 s is a pause (near this
        # segment's own quiet floor); never let one grow past 2× (no pause found)
        if self._pending_len >= self.segment_s * SR:
            floor = max(-90.0, float(np.percentile(self._pending_db, 10)))   # gated input: digital silence
            in_pause = max(self._pending_db[-5:]) < floor + 6.0
            if in_pause or self._pending_len >= 2 * self.segment_s * SR:
                self._submit_segment()

    def _submit_segment(self) -> None:
        if not self._pending:
            return
        seg = np.concatenate(self._pending)
        self._pending, self._pending_len, self._pending_db = [], 0, []
        ctx = self._dfn_context
        self._dfn_context = np.concatenate([ctx, seg])[-int(DFN_PREROLL_S * SR):]
        if self._dfn_stage_gain_db is None:
            self._dfn_stage_gain_db = deepfilter_input_gain_db(seg, ctx)
        self._futures.append(self._pool.submit(
            deepfilter_array, seg, SR, 100.0, 1, self.session.dir, ctx,
            stage_gain_db=self._dfn_stage_gain_db))
