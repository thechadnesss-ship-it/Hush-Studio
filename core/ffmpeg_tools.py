"""Thin, dependency-free wrappers around the FFmpeg / ffprobe CLIs.

Rules (CLAUDE.md §6): args are always lists, never shell=True; FFmpeg >= 6.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .config import FROZEN, bundle_root

MIN_FFMPEG_MAJOR = 6

# Don't flash a console window when called from the PyQt GUI on Windows.
_POPEN_FLAGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


class FFmpegError(RuntimeError):
    pass


# --------------------------------------------------------------------------- discovery

def _candidates(name: str) -> Iterable[Path]:
    exe = f"{name}.exe" if sys.platform == "win32" else name
    env_dir = os.environ.get("PIKA_FFMPEG_DIR")
    if env_dir:
        yield Path(env_dir) / exe
    yield bundled_tool_path(name)                # installed app / portable copy: tools/ffmpeg/
    found = shutil.which(name)
    if found:
        yield Path(found)
    # winget installs (Gyan.FFmpeg) only land on PATH after a shell restart.
    local = os.environ.get("LOCALAPPDATA")
    if local:
        pkgs = Path(local) / "Microsoft" / "WinGet" / "Packages"
        yield from sorted(pkgs.glob(f"Gyan.FFmpeg*/ffmpeg-*/bin/{exe}"), reverse=True)


def bundled_tool_path(name: str) -> Path:
    """Where the installer puts ffmpeg/ffprobe (build/hush_studio.spec bundles them there)."""
    exe = f"{name}.exe" if sys.platform == "win32" else name
    return bundle_root() / "tools" / "ffmpeg" / exe


@lru_cache(maxsize=None)
def find_tool(name: str) -> str:
    for cand in _candidates(name):
        if cand.is_file():
            return str(cand)
    if FROZEN:
        raise FFmpegError(
            f"Part of Hush Studio is missing ({name}). Please run the Hush Studio installer again "
            f"to repair it; your takes and settings are kept."
        )
    raise FFmpegError(
        f"{name} not found. Install FFmpeg >= {MIN_FFMPEG_MAJOR} "
        f"(e.g. `winget install Gyan.FFmpeg`) or set PIKA_FFMPEG_DIR."
    )


def ffmpeg_bin() -> str:
    return find_tool("ffmpeg")


def ffprobe_bin() -> str:
    return find_tool("ffprobe")


@lru_cache(maxsize=None)
def check_ffmpeg_version() -> str:
    """Return the version string; raise if older than MIN_FFMPEG_MAJOR."""
    out = run([ffmpeg_bin(), "-hide_banner", "-version"]).stdout
    m = re.search(r"ffmpeg version (\S+)", out)
    version = m.group(1) if m else "unknown"
    major = re.match(r"n?(\d+)\.", version)
    # Git/nightly builds ("N-12345-g...") are assumed new enough.
    if major and int(major.group(1)) < MIN_FFMPEG_MAJOR:
        raise FFmpegError(f"FFmpeg {version} is too old; need >= {MIN_FFMPEG_MAJOR}.")
    return version


# --------------------------------------------------------------------------- running

def run(args: Sequence[str], check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        list(args), capture_output=True, text=True, encoding="utf-8", errors="replace", **_POPEN_FLAGS
    )
    if check and proc.returncode != 0:
        raise FFmpegError(_fmt_failure(args, proc.returncode, proc.stderr))
    return proc


def _fmt_failure(args: Sequence[str], code: int, stderr: str) -> str:
    tail = "\n".join(stderr.strip().splitlines()[-15:])
    return f"{Path(args[0]).name} exited with {code}\n  args: {' '.join(map(str, args[1:]))}\n{tail}"


ProgressFn = Callable[[float], None]  # receives fraction 0.0..1.0


def run_with_progress(
    args: Sequence[str],
    total_seconds: float,
    on_progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> None:
    """Run ffmpeg with `-progress pipe:1` parsing. `args` must NOT include the binary
    or progress flags; they're added here. Safe to call from a QThread."""
    full = [ffmpeg_bin(), "-hide_banner", "-nostats", "-loglevel", "error", "-progress", "pipe:1", *args]
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errf:
        proc = subprocess.Popen(
            full, stdout=subprocess.PIPE, stderr=errf, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", **_POPEN_FLAGS,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            if cancel is not None and cancel.is_set():
                proc.kill()
                proc.wait()
                raise InterruptedError("cancelled")  # same as mp4_stitch, so callers catch one type
            if on_progress and line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue  # "N/A" before the first packet
                if total_seconds > 0:
                    on_progress(min(1.0, max(0.0, us / 1e6 / total_seconds)))
        code = proc.wait()
        if code != 0:
            errf.seek(0)
            raise FFmpegError(_fmt_failure(full, code, errf.read()))
    if on_progress:
        on_progress(1.0)


# --------------------------------------------------------------------------- probing

def probe(path: str | Path) -> dict:
    out = run([
        ffprobe_bin(), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path),
    ]).stdout
    return json.loads(out)


def duration(path: str | Path) -> float:
    """Container duration in seconds (honours MP4 edit lists, i.e. excludes AAC priming)."""
    info = probe(path)
    try:
        return float(info["format"]["duration"])
    except (KeyError, ValueError) as e:
        raise FFmpegError(f"Could not read duration of {path}") from e


@dataclass(frozen=True)
class AudioParams:
    codec: str
    profile: str
    sample_rate: int
    channels: int

    def compatible_with(self, other: "AudioParams") -> bool:
        return self == other


def audio_params(path: str | Path) -> AudioParams:
    info = probe(path)
    streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not streams:
        raise FFmpegError(f"No audio stream in {path}")
    s = streams[0]
    return AudioParams(
        codec=s.get("codec_name", ""),
        profile=s.get("profile", ""),
        sample_rate=int(s.get("sample_rate", 0)),
        channels=int(s.get("channels", 0)),
    )


def expected_audio_params(settings: dict) -> AudioParams:
    return AudioParams(
        codec=settings["codec"], profile="LC",
        sample_rate=int(settings["sample_rate"]), channels=int(settings["channels"]),
    )


# --------------------------------------------------------------------------- encoding

def aac_encode_args(settings: dict) -> list[str]:
    """Output args shared by every audio segment that goes into the concat (intro/session/tail)."""
    coder = ["-aac_coder", settings["aac_coder"]] if settings.get("aac_coder") and settings["codec"] == "aac" else []
    return [
        "-c:a", settings["codec"], *coder, "-b:a", str(settings["bitrate"]),
        "-ac", str(settings["channels"]), "-ar", str(settings["sample_rate"]),
        "-map_metadata", "-1",
    ]


def silence_split_points(y, sr: int, parts: int, min_run_s: float = 0.5) -> list[int]:
    """Up to parts−1 sample indices in the middle of digital-silence runs, near equal spacing."""
    import numpy as np
    if parts <= 1 or len(y) == 0:
        return []
    z = np.concatenate(([0], (np.asarray(y) == 0).astype(np.int8), [0]))
    d = np.diff(z)
    starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    keep = (ends - starts) >= int(min_run_s * sr)
    mids = ((starts[keep] + ends[keep]) // 2).astype(np.int64)
    if mids.size == 0:
        return []
    out: list[int] = []
    for k in range(1, parts):
        target = len(y) * k // parts
        m = int(mids[np.argmin(np.abs(mids - target))])
        if (not out or m > out[-1]) and 0 < m < len(y):
            out.append(m)
    return out


def encode_pcm_parallel(y, sr: int, dst: str | Path, settings: dict, workers: int | None = None) -> Path:
    """Encode float32 mono PCM → .m4a using several FFmpeg processes at once.

    FFmpeg's AAC encoder is single-threaded (~13–16 s per 30 min). The session is split in the
    middle of digital-silence pauses (Sam's edits are ~70% silence), the chunks are encoded in
    parallel to ADTS, byte-concatenated, and remuxed once. Each join adds ~21–43 ms of encoder
    priming/padding *inside a silence*: inaudible. No silence to split at → one process."""
    import numpy as np
    dst = Path(dst)
    y = np.ascontiguousarray(y, dtype=np.float32)
    workers = workers or max(1, os.cpu_count() or 1)
    cuts = [0, *silence_split_points(y, sr, workers), len(y)]
    tmpdir = Path(tempfile.mkdtemp(dir=dst.parent, prefix=".enc_"))
    try:
        procs, feeders, parts = [], [], []
        for i, (a, b) in enumerate(zip(cuts, cuts[1:])):
            part = tmpdir / f"part{i:02d}.aac"
            parts.append(part)
            p = subprocess.Popen(
                [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
                 *aac_encode_args(settings), "-f", "adts", str(part)],
                stdin=subprocess.PIPE, stderr=subprocess.PIPE, **_POPEN_FLAGS)
            chunk = memoryview(y[a:b]).cast("B")

            def feed(proc=p, data=chunk):
                try:
                    for off in range(0, len(data), 4 << 20):
                        proc.stdin.write(data[off: off + (4 << 20)])
                finally:
                    proc.stdin.close()
            t = threading.Thread(target=feed, daemon=True)
            t.start()
            procs.append(p)
            feeders.append(t)
        for p, t in zip(procs, feeders):
            t.join()
            err = p.stderr.read().decode("utf-8", "replace")
            if p.wait() != 0:
                raise FFmpegError(f"parallel AAC encode failed: {err[-800:]}")
        joined = tmpdir / "joined.aac"
        with open(joined, "wb") as out:
            for part in parts:
                with open(part, "rb") as f:
                    shutil.copyfileobj(f, out, 4 << 20)
        tmp = dst.with_name(dst.name + ".part")
        run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(joined),
             "-c", "copy", "-bsf:a", "aac_adtstoasc", "-map_metadata", "-1", "-f", "ipod", str(tmp)])
        os.replace(tmp, dst)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return dst


def encode_audio(src: str | Path, dst: str | Path, settings: dict, extra_input_args: Sequence[str] = (),
                 filters: str | None = None) -> Path:
    """Encode `src` -> `dst` (.m4a) atomically (write to temp, then rename)."""
    dst = Path(dst)
    tmp = dst.with_name(dst.name + ".part")
    args = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            *extra_input_args, "-i", str(src), "-vn"]
    if filters:
        args += ["-af", filters]
    args += [*aac_encode_args(settings), "-f", "ipod", str(tmp)]  # ipod muxer == .m4a
    run(args)
    os.replace(tmp, dst)
    return dst
