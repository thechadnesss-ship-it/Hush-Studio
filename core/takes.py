"""Takes library (D04): one folder per take in a Takes folder Sam can open in Explorer.

    Takes/<ts>/raw.flac, clean.wav, edits.json, capture.json, session.m4a, output_<h>h.mp4  (the session files)
    Takes/<ts>/take.json     name, created, notes, uploaded: the take's own metadata

The folder name is a timestamp and never changes; renaming only edits `name` in take.json, so a
rename can't break anything. Status is derived from the files: raw → edited → rendered → uploaded.
`.hushtake` is a zip of the folder (without the big rendered videos) for sharing.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from . import ffmpeg_tools as ff

META = "take.json"
EXPORT_EXT = ".hushtake"
AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".m4a", ".flac"})
EXPORT_AUDIO_EXTENSIONS = frozenset({".wav", ".flac", ".mp3"})
VIDEO_EXTENSIONS = frozenset({".mp4"})
STATUSES = ("raw", "edited", "rendered", "uploaded")
_NOT_COPIED = ("output_*.mp4", "output_*.mp4.tmp")      # big, rebuilt in seconds by Render


def _read_json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(p: Path, d: dict) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False), "utf-8")
    tmp.replace(p)


@dataclass
class Take:
    dir: Path
    name: str
    created: datetime
    length_s: float
    status: str
    notes: str = ""

    @property
    def id(self) -> str:
        return self.dir.name

    @property
    def rendered_files(self) -> list[Path]:
        return sorted(self.dir.glob("output_*h.mp4"))


def _length(d: Path) -> float:
    dur = _read_json(d / "edits.json").get("duration")
    if isinstance(dur, (int, float)):
        return float(dur)
    try:
        import soundfile as sf
        return float(sf.info(d / "raw.flac").duration)
    except Exception:
        return 0.0


def _created(d: Path, meta: dict) -> datetime:
    try:
        return datetime.fromisoformat(meta["created"])
    except (KeyError, ValueError, TypeError):
        pass
    try:
        return datetime.strptime(d.name[:17], "%Y-%m-%d_%H%M%S")
    except ValueError:
        return datetime.fromtimestamp(d.stat().st_mtime)


def read_take(d: Path) -> Take:
    meta = _read_json(d / META)
    if meta.get("uploaded"):
        status = "uploaded"
    elif any(d.glob("output_*h.mp4")):
        status = "rendered"
    elif (d / "edits.json").exists():
        status = "edited"
    else:
        status = "raw"
    return Take(d, str(meta.get("name") or d.name), _created(d, meta), _length(d), status, str(meta.get("notes", "")))


class TakeLibrary:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _dir(self, take: Take | str) -> Path:
        return take.dir if isinstance(take, Take) else self.root / take

    # ------------------------------------------------------------------ create / list

    def new_dir(self, name: str = "") -> Path:
        """A fresh take folder with its take.json. Never reuses a name (two takes in one second)."""
        now = datetime.now()
        base = now.strftime("%Y-%m-%d_%H%M%S")
        d, n = self.root / base, 1
        while d.exists():
            n += 1
            d = self.root / f"{base}_{n}"
        d.mkdir(parents=True)
        _write_json(d / META, {"name": name or now.strftime("Take %d %b %Y, %H:%M"),
                               "created": now.isoformat(timespec="seconds"), "notes": ""})
        return d

    def list(self, query: str = "") -> list[Take]:
        """Newest first. Folders without a take.json (older sessions) are listed too."""
        if not self.root.exists():
            return []
        takes = [read_take(d) for d in self.root.iterdir()
                 if d.is_dir() and ((d / "raw.flac").exists() or (d / META).exists())]
        q = query.strip().lower()
        if q:
            takes = [t for t in takes if q in t.name.lower() or q in t.notes.lower() or q in t.status
                     or q in t.created.strftime("%d %b %Y %Y-%m-%d").lower()]
        return sorted(takes, key=lambda t: t.created, reverse=True)

    def get(self, take_id: str) -> Take:
        d = self.root / take_id
        if not d.is_dir():
            raise FileNotFoundError(take_id)
        return read_take(d)

    # ------------------------------------------------------------------ manage

    def update_meta(self, take: Take | str, **fields) -> None:
        d = self._dir(take)
        meta = _read_json(d / META)
        meta.setdefault("created", _created(d, meta).isoformat(timespec="seconds"))
        meta.setdefault("name", d.name)
        meta.update(fields)
        _write_json(d / META, meta)

    def rename(self, take: Take | str, name: str) -> None:
        name = name.strip()
        if not name:
            raise ValueError("A take needs a name.")
        self.update_meta(take, name=name)

    def set_notes(self, take: Take | str, notes: str) -> None:
        self.update_meta(take, notes=notes)

    def mark_uploaded(self, take: Take | str, url: str = "") -> None:
        self.update_meta(take, uploaded=True, upload_url=url)

    def duplicate(self, take: Take | str) -> Take:
        """Copy of the take's working files (not the rendered videos, which Render rebuilds)."""
        src = self._dir(take)
        src_take = read_take(src)
        dst = self.new_dir()
        ignore = shutil.ignore_patterns(*_NOT_COPIED, META)
        for item in src.iterdir():
            if item.name in ignore(str(src), [item.name]):
                continue
            if item.is_dir():
                shutil.copytree(item, dst / item.name)
            else:
                shutil.copy2(item, dst / item.name)
        self.update_meta(dst, name=f"{src_take.name} (copy)", notes=src_take.notes)
        return read_take(dst)

    def delete(self, take: Take | str) -> None:
        """Permanent. The UI asks first. Refuses anything that isn't directly inside the Takes folder."""
        d = self._dir(take).resolve()
        if d.parent != self.root.resolve() or not d.is_dir():
            raise ValueError(f"{d} is not a take in {self.root}")
        shutil.rmtree(d)

    # ------------------------------------------------------------------ share

    def export(self, take: Take | str, dest: Path) -> Path:
        """Export the portable take bundle (raw audio, edit list and metadata).

        Rendered videos are deliberately left out: they are large and can be rebuilt from
        the encoded session audio without changing the take itself.
        """
        d = self._dir(take)
        dest = Path(dest)
        if dest.suffix != EXPORT_EXT:
            dest = dest.with_name(dest.name + EXPORT_EXT)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as z:       # audio is already compressed
            for f in sorted(d.rglob("*")):
                if f.is_file() and not f.match("output_*.mp4*"):
                    z.write(f, f.relative_to(d).as_posix())
        return dest

    def import_audio(self, path: Path, name: str | None = None) -> Take:
        """Create a take from a plain audio file through the session import path.

        ``core.session.import_take`` is the single audio-ingestion path.  It decodes to mono
        48 kHz and writes the lossless ``raw.flac`` backup, so Riverside's 44.1 kHz exports
        and files from other applications get the same take representation as recordings.
        Preparation (denoise/auto-edit) remains an explicit later step in the main window.
        """
        path = Path(path)
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            raise ValueError(f"Unsupported audio file: {path.name}")
        if not path.is_file():
            raise FileNotFoundError(path)

        from .session import Session, import_take

        take_name = (name or path.stem).strip() or "Imported take"
        d = self.new_dir(take_name)
        try:
            import_take(path, Session(d))
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise
        return read_take(d)

    def import_path(self, path: Path) -> Take:
        """Import either a ``.hushtake`` bundle or a supported plain audio file."""
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix == EXPORT_EXT:
            return self.import_export(path)
        if suffix in AUDIO_EXTENSIONS:
            return self.import_audio(path)
        raise ValueError(f"Unsupported take file: {path.name}")

    def _edited_audio(self, take: Take | str) -> np.ndarray:
        """Return the current edited timeline as mono 48 kHz float32 samples."""
        from .edl import EditList
        from .session import load_mono_48k

        d = self._dir(take)
        edits = d / "edits.json"
        if edits.exists():
            edl = EditList.load(edits)
            source = d / edl.source
            if not source.exists():
                raise FileNotFoundError(f"Edit-list source is missing: {source.name}")
            audio = load_mono_48k(source)
            return np.asarray(edl.render(audio), dtype=np.float32)

        raw = d / "raw.flac"
        if not raw.exists():
            raise FileNotFoundError(f"Take has no audio: {d}")
        return np.asarray(load_mono_48k(raw), dtype=np.float32)

    def export_audio(self, take: Take | str, dest: Path) -> Path:
        """Export the take's edited timeline as WAV, FLAC or MP3."""
        import soundfile as sf

        dest = Path(dest)
        if not dest.suffix:
            dest = dest.with_suffix(".wav")
        suffix = dest.suffix.lower()
        if suffix not in EXPORT_AUDIO_EXTENSIONS:
            raise ValueError("Edited audio must be exported as .wav, .flac or .mp3")

        audio = self._edited_audio(take)
        tmp = dest.with_name(dest.name + ".part")
        try:
            if suffix == ".mp3":
                proc = subprocess.run(
                    [ff.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
                     "-f", "f32le", "-ar", "48000", "-ac", "1", "-i", "pipe:0",
                     "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", str(tmp)],
                    input=audio.tobytes(), capture_output=True,
                )
                if proc.returncode:
                    detail = proc.stderr.decode("utf-8", "replace").strip()
                    raise RuntimeError(f"MP3 export failed: {detail[-800:]}")
            else:
                fmt = "WAV" if suffix == ".wav" else "FLAC"
                subtype = "FLOAT" if suffix == ".wav" else "PCM_24"
                sf.write(tmp, audio, 48000, format=fmt, subtype=subtype)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        return dest

    def export_video(self, take: Take | str, dest: Path, hours: float | None = None) -> Path:
        """Copy a finished MP4 out of the take without re-encoding it."""
        d = self._dir(take)
        if hours is None:
            rendered = sorted(d.glob("output_*h.mp4"))
            if not rendered:
                raise FileNotFoundError("This take has no finished MP4")
            source = rendered[-1]
        else:
            source = d / f"output_{hours:g}h.mp4"
            if not source.is_file():
                raise FileNotFoundError(source)
        dest = Path(dest)
        if not dest.suffix:
            dest = dest.with_suffix(".mp4")
        if dest.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError("Finished video must be exported as .mp4")
        shutil.copy2(source, dest)
        return dest

    def export_path(self, take: Take | str, dest: Path) -> Path:
        """Export using the destination extension selected by the user."""
        suffix = Path(dest).suffix.lower()
        if suffix == EXPORT_EXT:
            return self.export(take, dest)
        if suffix in EXPORT_AUDIO_EXTENSIONS:
            return self.export_audio(take, dest)
        if suffix in VIDEO_EXTENSIONS:
            return self.export_video(take, dest)
        raise ValueError("Choose a .hushtake, .wav, .flac, .mp3 or .mp4 destination")

    def import_export(self, path: Path) -> Take:
        d = self.new_dir()
        try:
            with zipfile.ZipFile(path) as z:
                for info in z.infolist():
                    target = (d / info.filename).resolve()
                    if d.resolve() not in target.parents and target != d.resolve():
                        raise ValueError(f"unsafe path in {Path(path).name}: {info.filename}")
                z.extractall(d)
            if not (d / "raw.flac").exists():
                raise ValueError(f"{Path(path).name} is not a Hush Studio take")
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise
        meta = _read_json(d / META)
        self.update_meta(d, name=str(meta.get("name") or d.name), uploaded=False)
        return read_take(d)

    # ------------------------------------------------------------------ explorer

    @staticmethod
    def show_in_explorer(take: Take | Path) -> list[str]:
        """Opens the take's folder (Explorer on Windows). Returns the command run."""
        d = take.dir if isinstance(take, Take) else Path(take)
        cmd = ["explorer", str(d)] if sys.platform == "win32" else ["xdg-open", str(d)]
        subprocess.Popen(cmd)
        return cmd


def migrate_sessions_folder(data_root: Path) -> Path:
    """Takes used to be `sessions/`. Rename it once (a move, nothing deleted) and return the Takes folder."""
    takes, old = Path(data_root) / "Takes", Path(data_root) / "sessions"
    if not takes.exists() and old.is_dir():
        old.rename(takes)
    takes.mkdir(parents=True, exist_ok=True)
    return takes
