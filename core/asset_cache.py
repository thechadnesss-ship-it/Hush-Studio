"""Builds and validates the media that is encoded ONCE and then stream-copied forever:

* the black video loop  (keyed by resolution + fps + length)
* the mastered intro    (keyed by hash of intro.wav bytes + mastering/audio settings)

Everything in cache/ is disposable: delete it and it is rebuilt on demand.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from . import ffmpeg_tools as ff
from .config import DATA_ROOT, RenderConfig

DEFAULT_CACHE_DIR = DATA_ROOT / "cache"

# Bump when the build recipe changes so old cache entries are ignored.
_BLACK_RECIPE_VERSION = 4
_INTRO_VIDEO_RECIPE_VERSION = 1
_INTRO_RECIPE_VERSION = 1

# (src_wav, dst_wav) -> None. Applies the mastering chain (Module C) to the intro.
MasterFn = Callable[[Path, Path], None]


def _short_hash(*parts: object) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(json.dumps(p, sort_keys=True, default=str).encode())
        h.update(b"\0")
    return h.hexdigest()[:12]


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class AssetCache:
    def __init__(self, cfg: RenderConfig, cache_dir: str | Path = DEFAULT_CACHE_DIR):
        self.cfg = cfg
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ black loop

    def black_loop_path(self) -> Path:
        c = self.cfg
        key = _short_hash(_BLACK_RECIPE_VERSION, c.resolution, c.loop_fps, c.black_loop_seconds)
        return self.dir / f"black_loop_{c.height}p_{c.loop_fps}fps_{key}.mp4"

    def black_loop(self) -> Path:
        """Return the cached black loop, building it (≈ a second) if missing/invalid."""
        path = self.black_loop_path()
        if path.exists() and self._black_loop_valid(path):
            return path
        c = self.cfg
        tmp = path.with_name(path.name + ".part")
        gop = c.loop_fps * 2  # keyframe every 60 frames (= 1 per minute once re-timed to 1 s frames)
        ff.run([
            ff.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s={c.resolution}:r={c.loop_fps}",
            "-t", str(c.black_loop_seconds),
            *self._x264_args(gop), "-f", "mp4", str(tmp),
        ])
        os.replace(tmp, path)
        if not self._black_loop_valid(path):
            raise ff.FFmpegError(f"Built black loop failed validation: {path}")
        return path

    def _x264_args(self, gop: int) -> list[str]:
        """The ONE H.264 recipe for every video piece (intro card and black loop), so their
        SPS/PPS (avcC) are byte-identical and mp4_stitch can put them in a single track.
        Verified: same args + same colour tags → identical avcC; `-tune stillimage` would
        change the PPS, and an untagged source changes the SPS colour description."""
        c = self.cfg
        return [
            "-vf", f"scale={c.resolution.replace('x', ':')}:flags=lanczos,fps={c.loop_fps},"
                   "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-profile:v", "high", "-level", "4.0",
            "-bf", "0",                     # no B-frames → no ctts, so mp4_stitch can byte-copy
            "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
            "-an", "-map_metadata", "-1",
        ]

    # ------------------------------------------------------------------ intro video (silent card)

    def intro_video_path(self, src: Path) -> Path:
        c = self.cfg
        key = _short_hash(_INTRO_VIDEO_RECIPE_VERSION, _file_hash(src), c.resolution, c.loop_fps)
        return self.dir / f"intro_video_{c.height}p_{c.loop_fps}fps_{key}.mp4"

    def intro_video(self, src: str | Path) -> Path:
        """Encode Sam's intro card once with the shared recipe (≈ a few seconds), then cache it.
        Any audio in the source is dropped: her voice starts under the card."""
        src = Path(src)
        if not src.exists():
            raise FileNotFoundError(f"Intro video not found: {src}")
        path = self.intro_video_path(src)
        if path.exists() and self._video_ok(path):
            return path
        tmp = path.with_name(path.name + ".part")
        ff.run([
            ff.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
            *self._x264_args(gop=self.cfg.loop_fps * 10), "-f", "mp4", str(tmp),
        ])
        os.replace(tmp, path)
        if not self._video_ok(path):
            raise ff.FFmpegError(f"Built intro video failed validation: {path}")
        return path

    def _video_ok(self, path: Path) -> bool:
        try:
            v = [s for s in ff.probe(path).get("streams", []) if s.get("codec_type") == "video"]
        except ff.FFmpegError:
            return False
        w, h = self.cfg.resolution.split("x")
        return bool(v) and v[0].get("codec_name") == "h264" and \
            (str(v[0].get("width")), str(v[0].get("height"))) == (w, h)

    def _black_loop_valid(self, path: Path) -> bool:
        try:
            info = ff.probe(path)
        except ff.FFmpegError:
            return False
        v = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
        if not v or v[0].get("codec_name") != "h264":
            return False
        w, h = self.cfg.resolution.split("x")
        if (str(v[0].get("width")), str(v[0].get("height"))) != (w, h):
            return False
        dur = float(info["format"].get("duration", 0))
        return abs(dur - self.cfg.black_loop_seconds) < 1.0

    # ------------------------------------------------------------------ intro

    def intro_path(self, intro_wav: Path, master_id: str) -> Path:
        key = _short_hash(
            _INTRO_RECIPE_VERSION, _file_hash(intro_wav), master_id,
            self.cfg.audio_settings(), self.cfg.target_lufs, self.cfg.seam_fade_ms,
        )
        return self.dir / f"intro_{key}.m4a"

    def intro(self, intro_wav: str | Path, master: MasterFn | None = None, master_id: str = "none") -> Path:
        """Return the mastered + encoded intro, building it if missing or if its codec
        params no longer match the session settings (concat-copy would fail)."""
        intro_wav = Path(intro_wav)
        if not intro_wav.exists():
            raise FileNotFoundError(f"Intro source not found: {intro_wav}")
        path = self.intro_path(intro_wav, master_id)
        if path.exists() and self.audio_matches(path):
            return path

        with tempfile.TemporaryDirectory(dir=self.dir) as td:
            src = intro_wav
            if master is not None:
                src = Path(td) / "intro_mastered.wav"
                master(intro_wav, src)
            ff.encode_audio(src, path, self.cfg.audio_settings())

        if not self.audio_matches(path):
            raise ff.FFmpegError(f"Encoded intro has unexpected codec params: {ff.audio_params(path)}")
        return path

    # ------------------------------------------------------------------ silence (exact-length padding)

    SILENCE_S = 120

    def silence(self) -> Path:
        """Digital silence in the session's AAC format, encoded once. Its frames pad the
        `exact` tail so the video ends at exactly the target length with zero encoding."""
        key = _short_hash("silence", 1, self.SILENCE_S, self.cfg.audio_settings())
        path = self.dir / f"silence_{self.SILENCE_S}s_{key}.m4a"
        if path.exists() and self.audio_matches(path):
            return path
        ff.run([ff.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"anullsrc=r={self.cfg.audio_sample_rate}:cl=mono",
                "-t", str(self.SILENCE_S), *ff.aac_encode_args(self.cfg.audio_settings()),
                "-f", "ipod", str(path.with_name(path.name + ".part"))])
        os.replace(path.with_name(path.name + ".part"), path)
        return path

    # ------------------------------------------------------------------ helpers

    def audio_matches(self, path: Path) -> bool:
        try:
            return ff.audio_params(path) == ff.expected_audio_params(self.cfg.audio_settings())
        except ff.FFmpegError:
            return False

    def clear(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)
