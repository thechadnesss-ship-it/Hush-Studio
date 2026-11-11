"""Small, testable helpers for the Audacity round trip (T14).

Audacity opens a WAV and a tab-separated label file passed on the same command
line as separate tracks.  The labels carry the information that cannot be
passed as a command-line option: cuts, pauses, the editor's markers, and the
position at which Sam left the cursor.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np

from .edl import EditList


def find_audacity() -> str | None:
    """Return an Audacity executable path, if the normal Windows installs have it."""
    for name in ("audacity", "audacity.exe"):
        exe = shutil.which(name)
        if exe:
            return exe

    bases = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
             os.environ.get("LOCALAPPDATA")]
    subdirs = ("Audacity/Audacity.exe", "Programs/Audacity/Audacity.exe")
    for base in bases:
        if not base:
            continue
        for sub in subdirs:
            path = Path(base) / sub
            if path.is_file():
                return str(path)
    return None


def _source_range(tl, start: float, end: float) -> tuple[float, float] | None:
    """Map a source range into output time, omitting ranges entirely cut away."""
    a, gone_a = tl.src_to_out(start * tl.sr)
    b, gone_b = tl.src_to_out(end * tl.sr)
    if gone_a and gone_b and abs(a - b) < 1e-9:
        return None
    return max(0.0, min(a, b) / tl.sr), min(tl.duration, max(a, b) / tl.sr)


def label_rows(edl: EditList, playhead_s: float = 0.0) -> list[tuple[float, float, str]]:
    """Build ``(start, end, label)`` rows for Audacity's label-track format.

    All positions are in the exported (edited) timeline, not the source file.
    Active cuts are retained as point labels at their join; proposed/kept cuts
    retain their visible range so Sam can see what is still under review.
    """
    tl = edl.timeline()
    rows: list[tuple[float, float, str]] = []

    for cut in edl.cuts:
        mapped = _source_range(tl, cut.start, cut.end)
        if mapped is None:
            # An active cut is intentionally absent from the output.  Its two
            # endpoints map to the join, which remains useful as a label.
            a, _ = tl.src_to_out(cut.start * tl.sr)
            b, _ = tl.src_to_out(cut.end * tl.sr)
            if abs(a - b) < 1e-6:
                p = max(0.0, min(tl.duration, a / tl.sr))
                rows.append((p, p, f"CUT: {cut.reason}"))
        else:
            a, b = mapped
            if not cut.keep:
                # Applied cuts usually map to a join, but a crossfade can leave
                # a tiny span.  Keep both endpoints when that happens.
                rows.append((a, b, f"CUT: {cut.reason}"))
            else:
                rows.append((a, b, f"CUT (kept/proposed): {cut.reason}"))

    for pause in edl.pauses:
        spans = [s for s in tl.vis if s.owner == pause.id and not s.ins]
        if spans:
            a = min(s.out0 for s in spans) / tl.sr
            b = max(s.end for s in spans) / tl.sr
            rows.append((a, b, f"PAUSE: {pause.kind}"))

    for marker in edl.markers:
        p, gone = tl.src_to_out(marker.t * tl.sr)
        if not gone:
            p = max(0.0, min(tl.duration, p / tl.sr))
            rows.append((p, p, f"MARKER: {marker.label or 'Marker'}"))

    p = max(0.0, min(tl.duration, float(playhead_s)))
    rows.append((p, p, "PLAYHEAD"))
    return sorted(rows, key=lambda row: (row[0], row[1], row[2]))


def write_label_track(path: Path, rows: list[tuple[float, float, str]]) -> Path:
    """Write an Audacity UTF-8 label track next to the exported audio."""
    path = Path(path)
    tmp = path.with_name(path.name + ".part")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            for start, end, label in rows:
                f.write(f"{max(0.0, start):.6f}\t{max(start, end):.6f}\t{label}\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def export_for_audacity(path: Path, audio: np.ndarray, edl: EditList,
                        playhead_s: float = 0.0) -> tuple[Path, Path]:
    """Export edited audio and its label track, returning both paths."""
    import soundfile as sf

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    try:
        sf.write(tmp, np.asarray(audio, dtype=np.float32), edl.sample_rate, format="WAV", subtype="FLOAT")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    labels = path.with_suffix(".txt")
    write_label_track(labels, label_rows(edl, playhead_s))
    return path, labels
