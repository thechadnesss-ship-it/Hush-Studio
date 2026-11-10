"""Debug bundle (T20): one zip Sam exports herself and hands over (flash drive, email, shared drive).

Always inside: recent logs, system info, settings (secrets removed), and for the chosen take its
capture.json plus the per-stage report from core.diagnostics. Audio only as clips she ticks
(`clip_ids`); the default is none. Nothing is ever uploaded by the app.

    python -m core.diagnostics bundle.zip      # prints the same stage report as for a session
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

from . import diagnostics as dg
from .app_info import APP_NAME, APP_VERSION
from .log_setup import LOG_NAME, log_dir, redact, system_info, audio_devices

CLIP_PAD_S = 3.0          # seconds either side of a flagged spot
MAX_CLIPS = 12
_SECRET_KEY = re.compile(r"(?i)token|secret|password|passwd|api[_-]?key|credential|client[_-]?(id|file)|authorization")

WHAT_IS_INCLUDED = (
    "Always included: the app's log files, this computer's basic details (Windows, Python, microphone "
    "list), your settings with any passwords or Google sign-in details removed, and for the take you "
    "pick: the recording check (device, clipping, lost sound) and a measurement table for each stage "
    "(loudness, peak, clipping, dropouts). No sound is included unless you tick a clip below. "
    "Nothing is sent anywhere; you choose where the file is saved and who gets it."
)


# ---------------------------------------------------------------- flagged spots → short clips

def _spots_from(x: np.ndarray, mask: np.ndarray, kind: str, stage: str, sr: int) -> list[dict]:
    s, e = dg._runs(mask)
    return [{"kind": kind, "stage": stage, "t_s": round(float(a) / sr, 2)} for a in s]


def flagged_spots(session_dir: str | Path) -> list[dict]:
    """Places worth hearing: clipping (raw), dropouts (clean) and lost sound (capture.json).
    Merged when within a clip's width, capped at MAX_CLIPS, each with a stable id."""
    d = Path(session_dir)
    found: list[dict] = []
    raw = d / "raw.flac"
    if raw.exists():
        x = dg._load(raw)
        found += _spots_from(x, np.abs(x) >= dg.CLIP, "clipping", "raw.flac", dg.SR)
    for name in ("clean.wav", "raw.flac"):
        if (d / name).exists():
            x = dg._load(d / name)
            found += [{"kind": "dropout", "stage": name, "t_s": t} for t in dg.zero_gaps(x)]
            break
    cap = d / "capture.json"
    if cap.exists():
        try:
            lost = json.loads(cap.read_text("utf-8")).get("lost_at_s", [])
        except (OSError, ValueError):
            lost = []
        src = "raw.flac" if raw.exists() else ("clean.wav" if (d / "clean.wav").exists() else None)
        if src:
            found += [{"kind": "lost sound", "stage": src, "t_s": float(t)} for t in lost]
    found.sort(key=lambda s: s["t_s"])
    merged: list[dict] = []
    for s in found:
        if merged and s["t_s"] - merged[-1]["t_s"] < 2 * CLIP_PAD_S:
            if s["kind"] not in merged[-1]["kind"].split(" + "):
                merged[-1]["kind"] += " + " + s["kind"]
            continue
        merged.append(dict(s))
    merged = merged[:MAX_CLIPS]
    for i, s in enumerate(merged):
        s["id"] = f"clip{i + 1:02d}_{int(s['t_s'])}s"
    return merged


def read_clip(session_dir: str | Path, spot: dict) -> tuple[np.ndarray, int]:
    """Audio around a spot (mono float32, 48 kHz) for playback and for the zip."""
    path = Path(session_dir) / spot["stage"]
    a = max(0.0, spot["t_s"] - CLIP_PAD_S)
    x = dg._load(path)
    seg = x[int(a * dg.SR): int((spot["t_s"] + CLIP_PAD_S) * dg.SR)]
    return seg.astype(np.float32), dg.SR


# ---------------------------------------------------------------- settings

def clean_settings(settings: dict) -> dict:
    out = {}
    for k, v in settings.items():
        if _SECRET_KEY.search(str(k)):
            continue
        out[str(k)] = redact(v) if isinstance(v, str) else v
    return out


# ---------------------------------------------------------------- build / read

def build_bundle(session_dir: str | Path | None, dest: str | Path, settings: dict | None = None,
                 clip_ids: list[str] | tuple[str, ...] = (), logs: str | Path | None = None,
                 ffmpeg_version: str | None = None) -> Path:
    """Write the zip. `dest` is a folder (file named automatically) or a .zip path."""
    dest = Path(dest)
    if dest.suffix.lower() != ".zip":
        dest.mkdir(parents=True, exist_ok=True)
        dest = dest / f"{APP_NAME.replace(' ', '')}-debug-{datetime.now():%Y-%m-%d_%H%M%S}.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    logs = Path(logs) if logs else log_dir()
    manifest: dict = {"app": f"{APP_NAME} {APP_VERSION}", "created": datetime.now().isoformat(timespec="seconds"),
                      "take": Path(session_dir).name if session_dir else None, "audio_clips": []}
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("system.json", json.dumps({"system": system_info(ffmpeg_version),
                                              "audio_devices": audio_devices()}, indent=2))
        z.writestr("settings.json", json.dumps(clean_settings(settings or {}), indent=2, default=str))
        for p in sorted(logs.glob(LOG_NAME + "*")) if logs.exists() else []:
            z.writestr(f"logs/{p.name}", redact(p.read_text("utf-8", errors="replace")))
        if session_dir:
            d = Path(session_dir)
            rep = dg.stage_report(d)
            rep["session"] = d.name                      # no folder paths from her PC
            z.writestr("take/report.json", json.dumps(rep, indent=2))
            z.writestr("take/report.txt", dg.format_report(rep))
            if rep["capture"] is not None:
                z.writestr("take/capture.json", json.dumps(rep["capture"], indent=2))
            wanted = set(clip_ids)
            for spot in (flagged_spots(d) if wanted else []):
                if spot["id"] in wanted:
                    seg, sr = read_clip(d, spot)
                    tmp = dest.with_suffix(".clip.tmp.flac")
                    sf.write(tmp, seg, sr, format="FLAC", subtype="PCM_24")
                    z.write(tmp, f"audio/{spot['id']}.flac")
                    tmp.unlink()
                    manifest["audio_clips"].append(spot)
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        z.writestr("README.txt", f"{APP_NAME} debug bundle.\n\n{WHAT_IS_INCLUDED}\n"
                                 "Read it with:  python -m core.diagnostics <this file>.zip\n")
    return dest


def read_bundle(path: str | Path) -> dict:
    """The stage report stored in a bundle (same dict shape as diagnostics.stage_report)."""
    with zipfile.ZipFile(path) as z:
        if "take/report.json" not in z.namelist():
            raise ValueError("this bundle has no take in it")
        return json.loads(z.read("take/report.json"))


# ---------------------------------------------------------------- where to save

def removable_drives() -> list[Path]:
    """Flash drives (Windows drive type 2), first in the save list; empty elsewhere."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        out = []
        for i in range(26):
            if mask >> i & 1:
                root = f"{chr(65 + i)}:\\"
                if ctypes.windll.kernel32.GetDriveTypeW(root) == 2 and Path(root).exists():
                    out.append(Path(root))
        return out
    except Exception:
        return []


def save_locations() -> list[tuple[str, Path]]:
    locs = [(f"Flash drive ({d})", d) for d in removable_drives()]
    home = Path.home()
    for label, p in (("Desktop", home / "Desktop"), ("Documents", home / "Documents")):
        if p.exists():
            locs.append((label, p))
    return locs
