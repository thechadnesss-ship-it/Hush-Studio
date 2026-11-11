"""Background noise removal (CLAUDE.md Module B).

Interface: `process(chunk) -> chunk` (streaming, any chunk size, fixed latency) and
`flush() -> tail`. Backends:

* **DeepFilterNet** (preferred, AI): the official Rust `deep-filter` CLI for offline files
  (`tools/deepfilter/deep-filter.exe`). The pip `deepfilternet` package has no Python 3.12
  Windows wheel (its Rust core won't build here), so the binary is the practical route.
  Live streaming can later use the same release's LADSPA DLL via ctypes.
* **SpectralGate** (built-in, numpy): streaming Wiener-style gate with a minimum-statistics
  noise estimate per frequency bin, plus a 70 Hz rumble cut. Removes steady room noise (hiss,
  hum, fans, distant traffic, ~15 dB); it can't remove a plane under speech like a neural model
  can, which is why the plane guard cuts those instead. Always available; the fallback.

`attenuation_limit_db` (per preset) caps how much noise is removed: lower keeps more room tone /
"vocal warmth" and avoids the over-processed sound.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
import soundfile as sf
from scipy.ndimage import minimum_filter1d
from scipy.signal import lfilter

from .config import PROJECT_ROOT

SR = 48000
DEEPFILTER_DIR = PROJECT_ROOT / "tools" / "deepfilter"


class Denoiser(Protocol):
    sample_rate: int
    latency_samples: int

    def process(self, chunk: np.ndarray) -> np.ndarray: ...
    def flush(self) -> np.ndarray: ...


class PassThrough:
    sample_rate = SR
    latency_samples = 0

    def process(self, chunk: np.ndarray) -> np.ndarray:
        return np.asarray(chunk, dtype=np.float32)

    def flush(self) -> np.ndarray:
        return np.zeros(0, dtype=np.float32)


@dataclass(frozen=True)
class SpectralGateConfig:
    frame: int = 960                 # 20 ms window, 10 ms hop at 48 kHz (DeepFilterNet's hop)
    noise_window_s: float = 1.5      # minimum-statistics window per bin
    psd_smooth: float = 0.9          # recursive smoothing of the power spectrum before the minimum
    noise_bias_db: float = 4.0       # min of the smoothed estimate under-reads the mean
    oversub: float = 1.5             # subtract a bit more than the estimate
    attenuation_limit_db: float = 18.0
    highpass_hz: float = 70.0        # rumble below her voice is always attenuated (Module C's HPF)
    gain_smooth: float = 0.6         # smoothing of the power the gain is computed from
    smooth: float = 0.3              # temporal smoothing of the gain itself (0 = none)


def _recursive_smooth(P: np.ndarray, a: float, prev: np.ndarray | None) -> np.ndarray:
    """y_j = a·y_{j-1} + (1−a)·x_j along axis 0, starting from `prev` (or y_0 = x_0)."""
    if a == 0:
        return P
    zi = (a * (P[0] if prev is None else prev))[None, :].astype(P.dtype)
    return lfilter(np.array([1 - a], P.dtype), np.array([1, -a], P.dtype), P, axis=0, zi=zi)[0]


class SpectralGate:
    """Streaming STFT gate: sqrt-Hann analysis/synthesis, 50% overlap, latency = 1 frame.

    Each `process()` call handles all its complete frames as one numpy batch: the per-bin time
    recursions are linear (lfilter with carried state) and the minimum-statistics window is a
    C sliding minimum over carried history. So it's exact for any chunk size, cheap for live
    100 ms chunks, and fast for whole files (33 min ≈ a few seconds instead of ~50 s per-frame)."""

    def __init__(self, cfg: SpectralGateConfig = SpectralGateConfig(), sample_rate: int = SR):
        assert cfg.frame % 2 == 0
        self.cfg = cfg
        self.sample_rate = sample_rate
        self.n = cfg.frame
        self.hop = cfg.frame // 2
        self.latency_samples = self.n - self.hop
        self.win = np.sqrt(np.hanning(self.n + 1)[:-1]).astype(np.float32)   # COLA at 50% overlap
        self.inbuf = np.zeros(self.n - self.hop, dtype=np.float32)
        self.ola_tail = np.zeros(self.hop, dtype=np.float32)
        nbins = self.n // 2 + 1
        self.W = max(1, int(round(cfg.noise_window_s * sample_rate / self.hop)))  # min-stat frames
        self.hist = np.zeros((0, nbins), dtype=np.float32)                         # last W−1 psm rows
        self.gain = np.ones(nbins, dtype=np.float32)
        self.psm: np.ndarray | None = None
        self.pfast: np.ndarray | None = None
        self.rumble = np.fft.rfftfreq(self.n, 1 / sample_rate) < cfg.highpass_hz
        self.floor = 10 ** (-cfg.attenuation_limit_db / 20)
        self.bias = 10 ** (cfg.noise_bias_db / 10)

    def process(self, chunk: np.ndarray) -> np.ndarray:
        x = np.concatenate([self.inbuf, np.asarray(chunk, dtype=np.float32).reshape(-1)])
        if len(x) < self.n:
            self.inbuf = x
            return np.zeros(0, dtype=np.float32)
        nfr = (len(x) - self.n) // self.hop + 1
        out = np.empty(nfr * self.hop, dtype=np.float32)
        block = 4096                                   # bound memory on whole-file calls
        for b0 in range(0, nfr, block):
            b1 = min(nfr, b0 + block)
            out[b0 * self.hop: b1 * self.hop] = self._frames(x, b0, b1)
        self.inbuf = x[nfr * self.hop:]
        return out

    def flush(self) -> np.ndarray:
        tail = self.process(np.zeros(self.n, dtype=np.float32))
        return tail[: self.latency_samples]

    def _frames(self, x: np.ndarray, j0: int, j1: int) -> np.ndarray:
        c, n, hop = self.cfg, self.n, self.hop
        idx = (np.arange(j0, j1) * hop)[:, None] + np.arange(n)[None, :]
        S = np.fft.rfft(x[idx] * self.win, axis=1).astype(np.complex64)
        # +1e-12 (≈ −120 dB): digital-silence pauses would otherwise drive the recursions into
        # denormal floats (~100x slower on x86), same trap as the loudness meter
        P = (S.real ** 2 + S.imag ** 2 + 1e-12).astype(np.float32)
        # smooth over time before taking the minimum: the min of raw periodograms of steady
        # noise sits ~20 dB under its mean; of smoothed ones only a few dB (→ noise_bias_db)
        psm = _recursive_smooth(P, c.psd_smooth, self.psm)
        self.psm = psm[-1]
        H = np.concatenate([self.hist, psm])
        # causal sliding minimum over the last W frames (window ends at the current frame)
        M = minimum_filter1d(H, size=self.W, axis=0, mode="nearest", origin=(self.W - 1) // 2)[-len(psm):]
        self.hist = H[-(self.W - 1):] if self.W > 1 else H[:0]
        noise = M * self.bias
        # gain from a lightly smoothed power, not the flickering raw periodogram: otherwise half
        # the noise bins randomly look like signal and leak through ("musical noise")
        pfast = _recursive_smooth(P, c.gain_smooth, self.pfast)
        self.pfast = pfast[-1]
        g = np.sqrt(np.clip(1.0 - c.oversub * noise / pfast, 0.0, 1.0), dtype=np.float32)
        g = np.maximum(g, np.float32(self.floor))
        g[:, self.rumble] = self.floor
        G = _recursive_smooth(g, c.smooth, self.gain) if c.smooth else g
        self.gain = G[-1]
        Y = (np.fft.irfft(S * G, n, axis=1) * self.win).astype(np.float32)
        # overlap-add: output hop j = second half of frame j−1 + first half of frame j
        k = len(Y)
        res = np.empty(k * hop, dtype=np.float32)
        res[:hop] = self.ola_tail + Y[0, :hop]
        if k > 1:
            res[hop:] = (Y[:-1, hop:] + Y[1:, :hop]).ravel()
        self.ola_tail = Y[-1, hop:].copy()
        return res


def denoise_array(x: np.ndarray, d: Denoiser, chunk: int = 4800) -> np.ndarray:
    """Run a streaming denoiser over a whole array, latency compensated (output aligned to input)."""
    parts = [d.process(x[i:i + chunk]) for i in range(0, len(x), chunk)]
    parts.append(d.flush())
    y = np.concatenate(parts)
    y = y[d.latency_samples:]
    if len(y) < len(x):
        y = np.concatenate([y, np.zeros(len(x) - len(y), dtype=np.float32)])
    return y[: len(x)]


# =========================================================================== DeepFilterNet (offline)

def deepfilter_exe() -> Path | None:
    exe = DEEPFILTER_DIR / ("deep-filter.exe" if sys.platform == "win32" else "deep-filter")
    if exe.is_file():
        return exe
    found = shutil.which("deep-filter")
    return Path(found) if found else None


def deepfilter_file(src: Path, dst: Path, attenuation_limit_db: float = 100.0,
                    post_filter: bool = False) -> Path:
    """Denoise a 48 kHz WAV with the official DeepFilterNet3 CLI (writes into a temp dir, then moves).

    Verified against the v0.5.6 CLI (`-a/--atten-lim-db`, `--pf`, `-D/--compensate-delay`, `-o`).
    It writes 16-bit PCM about 30 ms shorter than the input; `deepfilter_array` fixes that.
    """
    exe = deepfilter_exe()
    if exe is None:
        raise FileNotFoundError("DeepFilterNet not installed (tools/deepfilter/deep-filter.exe)")
    src, dst = Path(src), Path(dst)
    with tempfile.TemporaryDirectory(dir=dst.parent) as td:
        args = [str(exe), "-D", "-a", str(attenuation_limit_db), "-o", td]
        if post_filter:
            args.append("--pf")
        args.append(str(src))
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        proc = subprocess.run(args, capture_output=True, text=True, **flags)
        if proc.returncode != 0:
            raise RuntimeError(f"deep-filter failed ({proc.returncode}): {proc.stderr[-1500:]}")
        produced = list(Path(td).glob("*.wav"))
        if len(produced) != 1:
            raise RuntimeError(f"deep-filter produced {len(produced)} files")
        os.replace(produced[0], dst)
    return dst


def pause_split_points(x: np.ndarray, sr: int, parts: int, min_pause_s: float = 0.5) -> list[int]:
    """Up to parts−1 split indices in the middle of natural pauses (works on an ungated mic
    too, unlike digital-silence splitting), near equal spacing."""
    from .vad import detect_pauses
    mids = np.array([int((s + e) / 2 * sr) for s, e in detect_pauses(x, sr) if e - s >= min_pause_s])
    out: list[int] = []
    for k in range(1, parts):
        if mids.size == 0:
            break
        m = int(mids[np.argmin(np.abs(mids - len(x) * k // parts))])
        if (not out or m > out[-1]) and 0 < m < len(x):
            out.append(m)
    return out


DFN_PREROLL_S = 3.0       # context fed before each part, then discarded: joins match a whole-file run (≤ −58 dB)
DFN_TAIL_PAD_S = 0.1      # reflected audio fed after each part: the CLI's output is 30–40 ms short
DFN_HEADROOM = 0.9        # the CLI writes 16-bit PCM, which hard-clips at ±1.0
DFN_FLOOR_DBFS = -65.0    # white floor added to the CLI's input (it removes it again), see below
DFN_TARGET_PEAK_DBFS = -12.0
DFN_MAX_STAGE_GAIN_DB = 24.0
_DFN_FLOOR = np.random.default_rng(7).standard_normal(1 << 20).astype(np.float32)


def deepfilter_input_gain_db(x: np.ndarray, context: np.ndarray | None = None,
                             target_dbfs: float = DFN_TARGET_PEAK_DBFS) -> float:
    """Return a safe, relative gain for DeepFilterNet's input.

    The model has an absolute operating range. A quiet take can therefore make it treat the
    last, decaying part of a word as noise even though the same phrase at a normal level
    survives. Stage the model input toward a useful peak, but cap the gain so near-silence
    cannot become a loud noise source.
    """
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    peak = float(np.abs(x).max(initial=0.0))
    if context is not None:
        peak = max(peak, float(np.abs(np.asarray(context, dtype=np.float32)).max(initial=0.0)))
    if peak <= 0.0:
        return 0.0
    target = 10 ** (target_dbfs / 20)
    return float(np.clip(20 * np.log10(target / peak), 0.0, DFN_MAX_STAGE_GAIN_DB))


def deepfilter_array(x: np.ndarray, sr: int = SR, attenuation_limit_db: float = 100.0,
                     workers: int | None = None, work_dir: Path | None = None,
                     context: np.ndarray | None = None,
                     stage_gain_db: float | None = None) -> np.ndarray:
    """DeepFilterNet over an array, split at natural pauses and run on all cores at once
    (≈ 83 s single-process for 33 min on a 4-core i5). Returns float32 aligned to the input
    and exactly its length. Quiet inputs are staged toward ``DFN_TARGET_PEAK_DBFS`` before the
    CLI runs, then returned to their source level after denoising.

    Seamless joins (T7, measured on Sam's voice split mid-word): each part is fed with
    `DFN_PREROLL_S` of the audio before it (or `context`, the end of the previous live segment)
    and `DFN_TAIL_PAD_S` of its own audio, reflected, after it; then trimmed. Without this, every
    part ended in 30–40 ms of digital zero (the CLI drops 1440 samples + the last partial hop)
    and the next one restarted the model cold (up to 30 dB low for its first frames): a dropout
    click wherever a split landed in speech.
    The CLI's 16-bit output would also hard-clip any input above full scale (WASAPI delivers
    float, which can exceed ±1.0), so loud input is scaled into range and scaled back after.

    The CLI also outputs exact zeros for frames quieter than ≈ −72 dBFS *and for the 30 ms of
    real audio before them* (its delay compensation): on Sam's gated Riverside take, 88% of the
    384 phrase endings lost their last 30 ms. A −65 dBFS white floor on its input keeps every
    frame above that threshold; the model removes the floor again (pauses come out ≈ −97 dBFS)."""
    import concurrent.futures as cf
    workers = workers or max(1, os.cpu_count() or 1)
    x = np.asarray(x, dtype=np.float32)
    pre = int(DFN_PREROLL_S * sr)
    ctx = np.zeros(0, np.float32) if context is None or pre == 0 else np.asarray(context, np.float32)[-pre:]
    peak = float(max(np.abs(x).max(initial=0.0), np.abs(ctx).max(initial=0.0)))
    if stage_gain_db is None:
        stage_gain_db = deepfilter_input_gain_db(x, ctx)
    else:
        stage_gain_db = float(np.clip(stage_gain_db, 0.0, DFN_MAX_STAGE_GAIN_DB))
    stage_scale = np.float32(10 ** (stage_gain_db / 20))
    # Protect the CLI after the quiet-take gain stage. The inverse scale is applied to the
    # output below so denoising remains level-neutral for diagnostics and downstream mastering.
    scale = np.float32(DFN_HEADROOM / peak / stage_scale if peak * stage_scale > DFN_HEADROOM else stage_scale)
    # Keep the measured -65 dBFS floor needed for phrase tails. Raising it for a normal-level
    # take makes digital-silence pauses noisier and regresses the validated dropout fix.
    floor = np.float32(10 ** (DFN_FLOOR_DBFS / 20))
    off = len(ctx)                                 # x starts here in the virtual stream [ctx, x]

    def stream(lo: int, hi: int) -> np.ndarray:
        """[ctx, x][lo:hi], scaled, plus the floor (built per part: no full-take copies)."""
        seg = np.concatenate([ctx[lo:min(hi, off)], x[max(lo - off, 0):max(hi - off, 0)]]) * scale
        return seg + np.resize(np.roll(_DFN_FLOOR, -(lo % len(_DFN_FLOOR))), hi - lo) * floor

    cuts = [0, *pause_split_points(x, sr, workers), len(x)]
    pad = int(DFN_TAIL_PAD_S * sr)
    with tempfile.TemporaryDirectory(dir=work_dir) as td:
        td = Path(td)

        def one(i: int) -> np.ndarray:
            a, b = cuts[i] + off, cuts[i + 1] + off
            lead = min(pre, a)
            seg = stream(a - lead, b)
            # reflected, not zeros: zeros (or anything under ≈ −72 dBFS) blank the last 30 ms
            src, dst = td / f"in{i:02d}.wav", td / f"out{i:02d}.wav"
            sf.write(src, np.concatenate([seg, seg[max(0, len(seg) - pad):][::-1]]), sr, subtype="FLOAT")
            del seg
            deepfilter_file(src, dst, attenuation_limit_db)
            y, _ = sf.read(dst, dtype="float32")
            n = b - a
            y = y[lead:lead + n]
            return np.concatenate([y, np.zeros(max(0, n - len(y)), np.float32)])

        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            parts = list(ex.map(one, range(len(cuts) - 1)))
    y = np.concatenate(parts) if parts else np.zeros(0, np.float32)
    return y / scale if scale != 1.0 else y


def best_backend_name() -> str:
    return "deepfilternet" if deepfilter_exe() else "spectral_gate"
