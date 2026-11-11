"""Per-stage audio check of a session (T7): where does clipping or a dropout first appear?

    python -m core.diagnostics sessions/<timestamp>
    python -m core.diagnostics bundle.zip          # a debug bundle (T20), same report

Measures each stage file that exists (raw.flac → clean.wav → session.m4a → output_*.mp4) and
prints one line per stage, plus capture.json (what the mic delivered: device, clipping, lost
buffers). Plain text, so a take recorded on another PC can be diagnosed from a pasted report.

  peak      sample peak, dBFS
  lufs      integrated loudness (BS.1770)
  clipped   samples at/above 0.999 of full scale (a too-loud input, or a clipping stage)
  flat      runs of ≥ 8 identical samples near the file's own peak: clipping that was later
            turned down still shows as flat tops (PCM stages only; AAC smooths them)
  gaps      runs of 5–60 ms of exact digital zero inside sound: dropouts (clicks)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

from . import ffmpeg_tools as ff
from .master import integrated_lufs

SR = 48000
CLIP = 0.999


def _load(path: Path, max_s: float | None = None) -> np.ndarray:
    if path.suffix.lower() in (".flac", ".wav"):
        x, sr = sf.read(path, dtype="float32", always_2d=False,
                        frames=-1 if max_s is None else int(max_s * sf.info(path).samplerate))
        x = x.mean(axis=1) if x.ndim > 1 else x
        if sr == SR:
            return x
    args = [ff.ffmpeg_bin(), "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR)]
    if max_s is not None:
        args += ["-t", f"{max_s:.3f}"]
    r = subprocess.run(args + ["-f", "f32le", "-"], capture_output=True, check=True,
                       stdin=subprocess.DEVNULL, **ff._POPEN_FLAGS)
    return np.frombuffer(r.stdout, np.float32).copy()


def _runs(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return np.flatnonzero(d == 1), np.flatnonzero(d == -1)


def flat_top_runs(x: np.ndarray, min_len: int = 8, *, relative_to_peak: bool = True) -> int:
    """Count flat runs, optionally relative to the signal's own peak.

    The relative mode preserves the stage diagnostic's ability to find clipping
    that was later turned down.  Quality checks for an incoming take use
    ``relative_to_peak=False`` so a normally limited file is not mistaken for
    clipping merely because its loudest sample is below full scale.
    """
    a = np.abs(x)
    pk = float(a.max(initial=0.0))
    if pk <= 0:
        return 0
    level = 0.5 * pk if relative_to_peak else CLIP
    same = np.concatenate(([False], x[1:] == x[:-1])) & (a >= level)
    s, e = _runs(same)
    return int(np.count_nonzero(e - s >= min_len - 1))


def zero_gaps(x: np.ndarray, sr: int = SR, min_s: float = 0.005, max_s: float = 0.060,
              edge_db: float = -50.0) -> list[float]:
    """Start times of short exact-zero runs with sound on both sides (dropouts, not pauses)."""
    s, e = _runs(x == 0)
    keep = (e - s >= int(min_s * sr)) & (e - s <= int(max_s * sr)) & (s > 0) & (e < len(x))
    lim, w = 10 ** (edge_db / 20), int(0.010 * sr)
    out = []
    for a, b in zip(s[keep], e[keep]):
        if np.abs(x[max(0, a - w):a]).max() > lim and np.abs(x[b:b + w]).max() > lim:
            out.append(round(float(a) / sr, 3))
    return out


def measure(x: np.ndarray, sr: int = SR, pcm: bool = True) -> dict:
    a = np.abs(x)
    gaps = zero_gaps(x, sr)
    return {
        "dur_s": round(len(x) / sr, 2),
        "peak_dbfs": round(float(20 * np.log10(a.max(initial=1e-12))), 2),
        "lufs": round(integrated_lufs(x, sr), 2),
        "clipped": int(np.count_nonzero(a >= CLIP)),
        "flat": flat_top_runs(x) if pcm else None,
        "gaps": len(gaps),
        "first_gaps_s": gaps[:5],
    }


def stage_report(session_dir: str | Path) -> dict:
    d = Path(session_dir)
    stages: dict[str, dict] = {}
    take_s = None
    for name in ("raw.flac", "clean.wav"):
        if (d / name).exists():
            x = _load(d / name)
            take_s = take_s or len(x) / SR
            stages[name] = measure(x)
    if (d / "session.m4a").exists():
        x = _load(d / "session.m4a")
        take_s = len(x) / SR
        stages["session.m4a"] = measure(x, pcm=False)
    for mp4 in sorted(d.glob("output_*.mp4")):          # long: check the first loop only
        stages[mp4.name] = measure(_load(mp4, max_s=(take_s or 600) + 60), pcm=False)
    cap = d / "capture.json"
    return {"session": str(d), "capture": json.loads(cap.read_text("utf-8")) if cap.exists() else None,
            "stages": stages}


def format_report(rep: dict) -> str:
    lines = [f"Session {rep['session']}"]
    if rep["capture"]:
        lines.append("capture: " + ", ".join(f"{k}={v}" for k, v in rep["capture"].items()))
    else:
        lines.append("capture: (no capture.json: imported take, or recorded before T7)")
    lines.append(f"{'stage':<18}{'dur s':>9}{'peak':>8}{'LUFS':>8}{'clipped':>9}{'flat':>6}{'gaps':>6}  first gaps (s)")
    for name, m in rep["stages"].items():
        flat = "-" if m["flat"] is None else m["flat"]
        lines.append(f"{name:<18}{m['dur_s']:>9.2f}{m['peak_dbfs']:>8.1f}{m['lufs']:>8.1f}{m['clipped']:>9}"
                     f"{flat:>6}{m['gaps']:>6}  {m['first_gaps_s']}")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m core.diagnostics <session folder | bundle.zip>")
    if sys.argv[1].lower().endswith(".zip"):
        from .debug_bundle import read_bundle
        print(format_report(read_bundle(sys.argv[1])))
    else:
        print(format_report(stage_report(sys.argv[1])))
