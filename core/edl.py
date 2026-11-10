"""Edit decision list: the single, reviewable description of how a raw take becomes the session.

Everything that changes the take (plane cuts from the guard, flub cuts and pause shaping from
the auto-editor, Sam's manual tweaks) is an entry here. Nothing touches the raw audio until
`render()`, so every decision can be previewed, changed, or undone ("Keep anyway").
Serialises to JSON next to the session (sessions/<ts>/edits.json).
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

CUT_REASONS = ("plane", "flub", "noise", "manual")
PAUSE_KINDS = ("pause", "marker", "lead", "tail")


def _id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Cut:
    start: float
    end: float
    reason: str = "manual"
    keep: bool = False          # Sam's "Keep anyway": the cut is listed but not applied
    note: str = ""
    id: str = field(default_factory=_id)

    def __post_init__(self):
        if self.end <= self.start:
            raise ValueError(f"cut end {self.end} <= start {self.start}")
        if self.reason not in CUT_REASONS:
            raise ValueError(f"unknown cut reason {self.reason!r}")

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass
class Pause:
    start: float                # source time of the pause (between two phrases)
    end: float
    new_length: float           # length in the output (digital silence)
    locked: bool = False        # Sam set it by hand; auto-editor must not overwrite
    kind: str = "pause"         # "marker" = her spoken pause-marker word was here (removed)
    id: str = field(default_factory=_id)

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass
class EditList:
    duration: float
    sample_rate: int
    cuts: list[Cut] = field(default_factory=list)
    pauses: list[Pause] = field(default_factory=list)
    crossfade_s: float = 0.300          # equal-power crossfade at cut joins
    pause_edge_s: float = 0.030         # fade length into/out of digital-silence pauses
    silence_pauses: bool = True         # pauses become digital silence (Sam's edit style)

    # ------------------------------------------------------------------ queries

    def active_cuts(self) -> list[tuple[float, float]]:
        """Merged, applied cut ranges, clipped to the take."""
        rs = sorted((max(0.0, c.start), min(self.duration, c.end)) for c in self.cuts if not c.keep)
        out: list[list[float]] = []
        for s, e in rs:
            if e <= s:
                continue
            if out and s <= out[-1][1]:
                out[-1][1] = max(out[-1][1], e)
            else:
                out.append([s, e])
        return [(s, e) for s, e in out]

    def kept_ranges(self) -> list[tuple[float, float]]:
        out, t = [], 0.0
        for s, e in self.active_cuts():
            if s > t:
                out.append((t, s))
            t = e
        if t < self.duration:
            out.append((t, self.duration))
        return out

    def _pauses_in(self, a: float, b: float) -> list[Pause]:
        """Pauses lying inside a kept range (may touch its edges: lead/tail silence)."""
        e = self.pause_edge_s
        return sorted((p for p in self.pauses if p.start >= a - 1e-9 and p.end <= b + 1e-9 and p.length > 2 * e),
                      key=lambda p: p.start)

    def output_duration(self) -> float:
        kept = self.kept_ranges()
        total = sum(b - a for a, b in kept) - self.crossfade_s * max(0, len(kept) - 1)
        for a, b in kept:
            for p in self._pauses_in(a, b):
                total += p.new_length - p.length
        return max(0.0, total)

    def summary(self) -> dict:
        by: dict[str, list[Cut]] = {}
        for c in self.cuts:
            by.setdefault(c.reason, []).append(c)
        return {
            "cuts": {r: {"count": len(cs), "removed_s": round(sum(c.length for c in cs if not c.keep), 1),
                         "kept_anyway": sum(c.keep for c in cs)} for r, cs in by.items()},
            "pauses": len(self.pauses),
            "markers": sum(p.kind == "marker" for p in self.pauses),
            "source_s": round(float(self.duration), 1),
            "output_s": round(float(self.output_duration()), 1),
        }

    # ------------------------------------------------------------------ render

    def render(self, x: np.ndarray) -> np.ndarray:
        """Apply cuts (equal-power crossfades) and pause shaping to the source samples."""
        sr = self.sample_rate
        if abs(len(x) / sr - self.duration) > 0.05:
            raise ValueError(f"audio is {len(x)/sr:.2f}s but the edit list is for {self.duration:.2f}s")
        x = np.asarray(x, dtype=np.float32)
        pieces = [self._render_range(x, a, b) for a, b in self.kept_ranges()]
        if not pieces:
            return np.zeros(0, dtype=np.float32)
        xf = int(round(self.crossfade_s * sr))
        out = pieces[0]
        for p in pieces[1:]:
            out = _equal_power_join(out, p, xf)
        return out

    def _render_range(self, x: np.ndarray, a: float, b: float) -> np.ndarray:
        sr = self.sample_rate
        ef = int(round(self.pause_edge_s * sr))
        fade_out = np.cos(np.linspace(0, np.pi / 2, ef, dtype=np.float32)) ** 2 if ef else None
        fade_in = fade_out[::-1] if ef else None
        chunks: list[np.ndarray] = []
        t = int(round(a * sr))
        for p in self._pauses_in(a, b):
            ps, pe = int(round(p.start * sr)), int(round(p.end * sr))
            if not self.silence_pauses and abs(p.new_length - p.length) < 1e-6:
                continue  # untouched natural pause
            head = x[t:ps + ef].copy()
            if ef:
                head[-ef:] *= fade_out
            chunks.append(head)
            chunks.append(np.zeros(max(0, int(round(p.new_length * sr)) - 2 * ef), dtype=np.float32))
            tail = x[pe - ef:pe].copy()
            if ef:
                tail *= fade_in
            chunks.append(tail)
            t = pe
        chunks.append(x[t:int(round(b * sr))])
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    # ------------------------------------------------------------------ persistence

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, s: str) -> "EditList":
        d = json.loads(s)
        d["cuts"] = [Cut(**c) for c in d.get("cuts", [])]
        d["pauses"] = [Pause(**p) for p in d.get("pauses", [])]
        return cls(**d)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "EditList":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


def _equal_power_join(a: np.ndarray, b: np.ndarray, xf: int) -> np.ndarray:
    xf = min(xf, len(a), len(b))
    if xf <= 0:
        return np.concatenate([a, b])
    t = np.linspace(0, np.pi / 2, xf, dtype=np.float32)
    mid = a[-xf:] * np.cos(t) + b[:xf] * np.sin(t)
    return np.concatenate([a[:-xf], mid, b[xf:]])
