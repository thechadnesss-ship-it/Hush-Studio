"""Live recording (CLAUDE.md Module B), with the plane guard running live.

    mic callback ──put_nowait──► queue ──► worker thread:
                                           • raw.flac (always: the untouched backup)
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
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import numpy as np
import soundfile as sf

from .denoise import SpectralGate, deepfilter_array, deepfilter_exe
from .plane_guard.ear import Ear
from .plane_guard.guard import Guard, GuardUpdate
from .plane_guard.sky_radar import Home, SkyRadar
from .session import Session

SR = 48000
BLOCK = 4800                     # 100 ms


# =========================================================================== sources

class AudioSource(Protocol):
    def start(self, on_block: Callable[[np.ndarray], None]) -> None: ...
    def stop(self) -> None: ...


class MicSource:
    """sounddevice input at 48 kHz mono float32. `device` = index or name substring (None = default)."""

    def __init__(self, device: int | str | None = None):
        self.device = device
        self.stream = None
        self.overflows = 0

    def start(self, on_block):
        import sounddevice as sd

        def cb(indata, frames, t, status):
            if status.input_overflow:
                self.overflows += 1
            on_block(indata[:, 0].copy())          # copy + enqueue only

        self.stream = sd.InputStream(device=self.device, samplerate=SR, channels=1, dtype="float32",
                                     blocksize=BLOCK, callback=cb)
        self.stream.start()

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
                 on_radar: Callable | None = None):
        from .plane_guard.guard import GuardConfig
        self.session = session
        self.source = source or MicSource()
        self.ear = Ear(SR)
        self.guard = Guard(self.ear, cfg=GuardConfig(enabled=guard_enabled))
        self.radar = radar if radar is not None else (SkyRadar(home) if use_radar else None)
        self.segment_s = segment_s
        self.on_status = on_status
        self.on_radar = on_radar             # e.g. the quiet-time planner's logger
        self.status = LiveStatus()
        self.q: queue.Queue[np.ndarray] = queue.Queue(maxsize=600)   # 60 s of slack
        self._lock = threading.Lock()                                # guard is touched by 2 threads
        self._paused = False
        self._running = threading.Event()
        self._worker: threading.Thread | None = None
        self._radar_t: threading.Thread | None = None
        self._samples = 0
        # noise removal
        self.use_dfn = deepfilter_exe() is not None
        self.status.denoise_backend = "DeepFilterNet" if self.use_dfn else "SpectralGate"
        self._gate = None if self.use_dfn else SpectralGate()
        self._clean_parts: list[np.ndarray] = []
        self._pending: list[np.ndarray] = []        # raw blocks of the current DFN segment
        self._pending_len = 0
        self._pending_db: list[float] = []
        self._futures: list[cf.Future] = []
        self._pool = cf.ThreadPoolExecutor(max_workers=2) if self.use_dfn else None
        self._raw_file: sf.SoundFile | None = None
        self._recent = collections.deque(maxlen=int(30 * SR / BLOCK))   # last 30 s for "listen back"

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        self.session.dir.mkdir(parents=True, exist_ok=True)
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
        return RecordingResult(self.session.raw, clean, list(self.ear.events), self.take_s,
                               self.status.denoise_backend, list(self.guard.history))

    # ------------------------------------------------------------------ threads

    def _enqueue(self, block: np.ndarray) -> None:      # runs in the audio callback
        try:
            self.q.put_nowait(block)
        except queue.Full:
            self.status.dropped_blocks += 1

    def _work(self) -> None:
        while self._running.is_set() or not self.q.empty():
            try:
                b = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            rms = float(np.sqrt(np.mean(b.astype(np.float64) ** 2)) + 1e-12)
            self.status.level_db = 20 * np.log10(rms)
            self.status.peak_db = float(20 * np.log10(np.abs(b).max(initial=1e-12)))
            self.status.paused = self._paused
            if self._paused:
                self._publish(None)
                continue
            self._raw_file.write(b)
            self._recent.append(b)
            self._samples += len(b)
            with self._lock:
                self.ear.feed(b)
                upd = self.guard.tick(self.take_s)
            self._denoise(b)
            self._publish(upd)

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
        self._futures.append(self._pool.submit(deepfilter_array, seg, SR, 100.0, 1, self.session.dir))
