"""Intro manager (T15): Sam's silent intro cards, kept as a small library with one *active* intro.

Layout: ``Intros/intros.json`` + ``Intros/<id>/card.<ext>``. Uploading a video copies it; an image
becomes a still-card video (``IMAGE_CARD_S`` long). The render uses ``active_path()``. The cache
(``AssetCache.intro_video``) is hash-keyed on the file bytes, so changing the active intro rebuilds
the cached intro video automatically, and old intros (and their cache entries) stay available.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from . import ffmpeg_tools as ff

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v", ".webm", ".avi"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
FILE_FILTER = "Intro card (*.mp4 *.mov *.mkv *.m4v *.webm *.avi *.png *.jpg *.jpeg *.bmp *.webp)"
MIN_W, MIN_H = 1280, 720
MIN_S, MAX_S = 1.0, 120.0
IMAGE_CARD_S = 10.0
ASPECT = 16 / 9


class IntroError(Exception):
    """Message is written for Sam, not for a developer."""


@dataclass
class Intro:
    id: str
    name: str
    file: str            # relative to the library root
    kind: str            # "video" | "image"
    created: float
    duration_s: float
    width: int
    height: int
    fps: float


def _fps(s: dict) -> float:
    try:
        a, b = str(s.get("avg_frame_rate", "0/1")).split("/")
        return float(a) / float(b) if float(b) else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def validate_intro_file(path: str | Path) -> dict:
    """Return {kind, duration_s, width, height, fps} or raise IntroError with a friendly reason."""
    p = Path(path)
    if not p.is_file():
        raise IntroError("That file could not be found.")
    ext = p.suffix.lower()
    if ext not in VIDEO_EXTS | IMAGE_EXTS:
        raise IntroError(f"“{p.name}” isn't a video or picture I can use. Choose an MP4/MOV video or a PNG/JPG picture.")
    try:
        info = ff.probe(p)
    except Exception:
        raise IntroError(f"“{p.name}” couldn't be opened. It may be damaged or not really a "
                         f"{'picture' if ext in IMAGE_EXTS else 'video'}.")
    vs = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if not vs:
        raise IntroError(f"“{p.name}” has no picture in it.")
    v = vs[0]
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    if w < MIN_W or h < MIN_H:
        raise IntroError(f"The intro is {w}×{h}. It must be at least {MIN_W}×{MIN_H} (ideally 1920×1080).")
    if abs(w / h - ASPECT) > 0.03 * ASPECT:
        raise IntroError(f"The intro is {w}×{h}, which isn't widescreen (16:9). It would be stretched or cropped; "
                         "please use a 1920×1080 card.")
    if ext in IMAGE_EXTS:
        return dict(kind="image", duration_s=IMAGE_CARD_S, width=w, height=h, fps=0.0)
    try:
        dur = float(info["format"]["duration"])
    except (KeyError, ValueError):
        raise IntroError(f"I couldn't tell how long “{p.name}” is.")
    fps = _fps(v)
    if dur < MIN_S:
        raise IntroError(f"The intro is only {dur:.1f} s long. It needs to be at least {MIN_S:.0f} s.")
    if dur > MAX_S:
        raise IntroError(f"The intro is {dur / 60:.1f} minutes long. Intro cards are short: {MAX_S:.0f} s at most.")
    if fps < 1:
        raise IntroError("I couldn't read the frame rate of that video.")
    return dict(kind="video", duration_s=dur, width=w, height=h, fps=fps)


class IntroLibrary:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._db = self.root / "intros.json"
        self._items: list[Intro] = []
        self._active = ""
        self._load()

    # ---- persistence
    def _load(self):
        try:
            d = json.loads(self._db.read_text(encoding="utf-8"))
            self._items = [Intro(**i) for i in d.get("intros", [])]
            self._active = d.get("active", "")
        except (OSError, ValueError, TypeError):
            self._items, self._active = [], ""
        if self._active not in {i.id for i in self._items}:
            self._active = ""

    def _save(self):
        tmp = self._db.with_name("intros.json.part")
        tmp.write_text(json.dumps({"active": self._active, "intros": [asdict(i) for i in self._items]}, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self._db)

    # ---- queries
    def list(self) -> list[Intro]:
        return list(self._items)

    def get(self, intro_id: str) -> Intro | None:
        return next((i for i in self._items if i.id == intro_id), None)

    def path(self, intro: Intro) -> Path:
        return self.root / intro.file

    @property
    def active_id(self) -> str:
        return self._active

    def active(self) -> Intro | None:
        return self.get(self._active)

    def active_path(self) -> Path | None:
        a = self.active()
        if a and self.path(a).exists():
            return self.path(a)
        return None

    # ---- changes
    def add(self, src: str | Path, name: str = "", make_active: bool = False) -> Intro:
        src = Path(src)
        meta = validate_intro_file(src)
        iid = uuid.uuid4().hex[:8]
        folder = self.root / iid
        folder.mkdir()
        try:
            if meta["kind"] == "image":
                dst = folder / "card.mp4"
                self._image_card(src, dst)
            else:
                dst = folder / f"card{src.suffix.lower()}"
                shutil.copy2(src, dst)
        except Exception as e:
            shutil.rmtree(folder, ignore_errors=True)
            if isinstance(e, IntroError):
                raise
            raise IntroError(f"I couldn't save that intro: {e}") from e
        intro = Intro(id=iid, name=name.strip() or src.stem, file=f"{iid}/{dst.name}", kind=meta["kind"],
                      created=time.time(), duration_s=meta["duration_s"], width=meta["width"],
                      height=meta["height"], fps=meta["fps"])
        self._items.append(intro)
        if make_active or not self._active:
            self._active = iid
        self._save()
        return intro

    @staticmethod
    def _image_card(src: Path, dst: Path):
        try:
            ff.run([ff.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-framerate", "30",
                    "-i", str(src), "-t", str(IMAGE_CARD_S), "-vf", "scale=1920:1080,format=yuv420p",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-an", str(dst)])
        except ff.FFmpegError as e:
            raise IntroError("I couldn't turn that picture into an intro card.") from e

    def rename(self, intro_id: str, name: str):
        i = self.get(intro_id)
        if i and name.strip():
            i.name = name.strip()
            self._save()

    def set_active(self, intro_id: str):
        if self.get(intro_id) is None:
            raise IntroError("That intro no longer exists.")
        self._active = intro_id
        self._save()

    def clear_active(self):
        """Render with no intro card (older intros are kept)."""
        self._active = ""
        self._save()

    def remove(self, intro_id: str):
        i = self.get(intro_id)
        if i is None:
            return
        if intro_id == self._active:
            raise IntroError("This is the active intro. Choose another one first.")
        shutil.rmtree(self.root / intro_id, ignore_errors=True)
        self._items.remove(i)
        self._save()
