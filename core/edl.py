"""Edit decision list: the single, reviewable description of how a raw take becomes the session.

Everything that changes the take (plane cuts from the guard, flub cuts and pause shaping from
the auto-editor, Sam's manual edits in the waveform editor) is an entry here. Nothing touches the
audio file until `render()`, so every decision can be previewed, changed, or undone.
Serialises to JSON next to the take (Takes/<ts>/edits.json).

Entries (all in *source* time, i.e. seconds of the audio file named by `source`):

* `Cut`     a source range left out; joined with an equal-power crossfade (`xfade_s`, default
            `crossfade_s`; Sam's own cuts use a short one so what she sees is what she gets).
* `Pause`   a source range replaced by digital silence of `new_length` seconds.
* `Insert`  pasted audio (copies of source ranges) and/or inserted silence, placed at a source time.
* `Gain`    a level change and/or fade over a source range (or inside one insert): Normalise, fades.
* `Marker`  a labelled point, for finding places again. No effect on the sound.

`timeline()` turns the entries into the *output* timeline, sample-exact. It is the one place that
decides what the session sounds like: `render()`, the editor's waveform, playback and the mapping
between what Sam sees (output time) and the file (source time) all come from it.
"""
from __future__ import annotations

import bisect
import json
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import accumulate
from pathlib import Path

import numpy as np

CUT_REASONS = ("plane", "flub", "noise", "manual")
PAUSE_KINDS = ("pause", "marker", "lead", "tail", "silence")
FADES = ("none", "in", "out")
INSERT_EDGE_S = 0.005          # tiny fades where inserted audio meets the take (no clicks)
GAIN_RAMP_S = 0.020            # a level change eases in/out over this, never a step
DEFAULT_SOURCE = "clean.wav"


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
    proposed: bool = False      # auto-proposal Sam has not decided on yet; not applied (keep stays True)
    xfade_s: float | None = None    # crossfade at this join; None = the list's crossfade_s

    def accept(self) -> None:
        self.proposed, self.keep = False, False

    def undo(self) -> None:
        self.proposed, self.keep = False, True

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
    kind: str = "pause"         # "marker" = her spoken pause-marker word was here (removed); "silence" = set by hand
    id: str = field(default_factory=_id)

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass
class Insert:
    """Audio or silence that is not in the file at this point: a paste, or "insert silence"."""
    at: float                   # source time it goes in front of
    clips: list[list] = field(default_factory=list)   # [src_start, src_end] = audio; [None, seconds] = silence
    note: str = ""
    id: str = field(default_factory=_id)

    @property
    def length(self) -> float:
        return float(sum(b if a is None else b - a for a, b in self.clips))


@dataclass
class Gain:
    """Level change (`db`) and/or fade over [start, end]. Outside the range nothing changes."""
    start: float
    end: float
    db: float = 0.0
    fade: str = "none"          # "in": silent → full, "out": full → silent, across the range
    reason: str = "manual"      # "normalise" | "fade" | "manual"
    on: str | None = None       # an Insert id: times are seconds inside that insert, not source time
    id: str = field(default_factory=_id)

    def __post_init__(self):
        if self.end <= self.start:
            raise ValueError(f"gain end {self.end} <= start {self.start}")
        if self.fade not in FADES:
            raise ValueError(f"unknown fade {self.fade!r}")

    def envelope(self, t0: float, t1: float, n: int) -> float | np.ndarray | None:
        """Gain over n samples spanning [t0, t1): None if untouched, a scalar if constant."""
        if t1 <= self.start or t0 >= self.end or n <= 0:
            return None
        ramp = min(GAIN_RAMP_S, (self.end - self.start) / 2)
        flat = self.fade == "none" and t0 >= self.start + ramp and t1 <= self.end - ramp
        if flat:
            return float(10 ** (self.db / 20))
        t = t0 + (np.arange(n, dtype=np.float64) * ((t1 - t0) / n))
        inside = (t >= self.start) & (t < self.end)
        g = np.ones(n, dtype=np.float64)
        ti = t[inside]
        u = (ti - self.start) / (self.end - self.start)
        if self.fade == "in":
            shape = np.sin(np.pi / 2 * u) ** 2
        elif self.fade == "out":
            shape = np.cos(np.pi / 2 * u) ** 2
        else:
            shape = 1.0
        db = self.db
        if ramp > 0 and db:
            db = db * np.clip(np.minimum(ti - self.start, self.end - ti) / ramp, 0.0, 1.0)
        g[inside] = 10 ** (db / 20) * shape
        return g.astype(np.float32)


@dataclass
class Marker:
    t: float                    # source time
    label: str = ""
    id: str = field(default_factory=_id)


# ===================================================================== output timeline

@dataclass(slots=True)
class Seg:
    """One stretch of the output. Audio: output sample out0+k = source sample src0+k."""
    out0: int
    n: int
    kind: str                   # "audio" | "silence"
    src0: int                   # audio: first source sample; silence: source position its start stands for
    src1: int                   # audio: src0 + n; silence: source position its end stands for
    owner: str | None = None    # Pause id (its silence) or Insert id (anything inside that insert)
    ins: bool = False           # part of an Insert (not the take's own timeline)
    home: int = 0               # inserts: source position they sit at
    local0: int = 0             # inserts: sample offset inside the insert
    fin: int = 0                # cos² fade-in over the first fin samples
    fout: int = 0               # cos² fade-out over the last fout samples
    piece: int = 0

    @property
    def end(self) -> int:
        return self.out0 + self.n


@dataclass(slots=True)
class Piece:
    """A kept stretch of the take (between cuts); neighbours overlap by the crossfade."""
    out0: int
    n: int
    xf_in: int = 0
    xf_out: int = 0

    @property
    def end(self) -> int:
        return self.out0 + self.n


@lru_cache(maxsize=64)
def _fade_out(n: int) -> np.ndarray:
    return np.cos(np.linspace(0, np.pi / 2, n, dtype=np.float32)) ** 2


@lru_cache(maxsize=64)
def _xcos(n: int) -> np.ndarray:
    return np.cos(np.linspace(0, np.pi / 2, n, dtype=np.float32))


@lru_cache(maxsize=64)
def _xsin(n: int) -> np.ndarray:
    return np.sin(np.linspace(0, np.pi / 2, n, dtype=np.float32))


class Timeline:
    """The output as a list of segments (sample-exact). Built by `EditList.timeline()`."""

    def __init__(self, sr: int, segs: list[Seg], pieces: list[Piece], gains: list[Gain]):
        self.sr = sr
        self.pieces = pieces
        self.segs = sorted(segs, key=lambda s: s.out0)
        self.total = pieces[-1].end if pieces else 0
        self._maxend = list(accumulate((s.end for s in self.segs), max))
        # visible (non-overlapping) view: in a crossfade the later piece is the one shown
        by_piece: list[list[Seg]] = [[] for _ in pieces]
        for s in segs:
            by_piece[s.piece].append(s)
        vis: list[Seg] = []
        hidden: list[Seg] = []          # an earlier piece's tail under a crossfade (heard, not shown)
        for k, p in enumerate(pieces):
            stop = pieces[k + 1].out0 if k + 1 < len(pieces) else p.end
            for s in by_piece[k]:
                if s.out0 >= stop:
                    hidden.append(s)
                    continue
                if s.end > stop:
                    cut = stop - s.out0
                    audio = s.kind == "audio"
                    hidden.append(Seg(stop, s.n - cut, s.kind, s.src0 + cut if audio else s.src1, s.src1,
                                      s.owner, s.ins, s.home, s.local0 + cut, 0, s.fout, s.piece))
                    s = Seg(s.out0, cut, s.kind, s.src0, s.src0 + cut if audio else s.src1,
                            s.owner, s.ins, s.home, s.local0, s.fin, 0, s.piece)
                vis.append(s)
        vis.sort(key=lambda s: s.out0)
        self.vis = vis
        self.hidden = hidden
        self._vstarts = [s.out0 for s in vis]
        self._srcsegs = [s for s in vis if not s.ins]
        self._srcstarts = [s.src0 for s in self._srcsegs]
        self._gsrc = [g for g in gains if g.on is None]
        self._gins: dict[str, list[Gain]] = {}
        for g in gains:
            if g.on is not None:
                self._gins.setdefault(g.on, []).append(g)

    @property
    def duration(self) -> float:
        return self.total / self.sr

    # ------------------------------------------------------------------ audio

    def _gain(self, s: Seg, k0: int, k1: int):
        if s.ins:
            gl, base = self._gins.get(s.owner, ()), s.local0
        else:
            gl, base = self._gsrc, s.src0
        if not gl:
            return None
        t0, t1 = (base + k0) / self.sr, (base + k1) / self.sr
        out = None
        for g in gl:
            e = g.envelope(t0, t1, k1 - k0)
            if e is not None:
                out = e if out is None else out * e
        return out

    def render(self, x: np.ndarray, a: int = 0, b: int | None = None) -> np.ndarray:
        """Output samples [a, b) (defaults: everything)."""
        b = self.total if b is None else min(b, self.total)
        a = max(0, a)
        out = np.zeros(max(0, b - a), dtype=np.float32)
        if b <= a:
            return out
        i = bisect.bisect_right(self._maxend, a)
        segs = self.segs
        while i < len(segs) and segs[i].out0 < b:
            s = segs[i]
            i += 1
            if s.kind != "audio":
                continue
            lo, hi = max(a, s.out0), min(b, s.end)
            if hi <= lo:
                continue
            k0, k1 = lo - s.out0, hi - s.out0
            y = x[s.src0 + k0: s.src0 + k1]
            if len(y) < hi - lo:                       # file shorter than the list expects: leave silence
                hi = lo + len(y)
                k1 = k0 + len(y)
                if hi <= lo:
                    continue
            w = None
            if s.fin and k0 < s.fin:
                f = _fade_out(s.fin)[::-1][k0:min(k1, s.fin)]
                w = np.ones(k1 - k0, dtype=np.float32)
                w[:len(f)] *= f
            if s.fout and k1 > s.n - s.fout:
                j0 = max(k0, s.n - s.fout)
                f = _fade_out(s.fout)[j0 - (s.n - s.fout): k1 - (s.n - s.fout)]
                if w is None:
                    w = np.ones(k1 - k0, dtype=np.float32)
                w[j0 - k0:] *= f
            p = self.pieces[s.piece]
            if p.xf_in and lo < p.out0 + p.xf_in:
                e = min(hi, p.out0 + p.xf_in)
                if w is None:
                    w = np.ones(k1 - k0, dtype=np.float32)
                w[:e - lo] *= _xsin(p.xf_in)[lo - p.out0: e - p.out0]
            if p.xf_out and hi > p.end - p.xf_out:
                st = max(lo, p.end - p.xf_out)
                if w is None:
                    w = np.ones(k1 - k0, dtype=np.float32)
                w[st - lo:] *= _xcos(p.xf_out)[st - (p.end - p.xf_out): hi - (p.end - p.xf_out)]
            g = self._gain(s, k0, k1)
            if g is not None:
                w = (np.full(k1 - k0, g, dtype=np.float32) if np.isscalar(g) else g) if w is None else w * g
            out[lo - a: hi - a] += y if w is None else y * w
        return out

    # ------------------------------------------------------------------ mapping

    def seg_index(self, o: float) -> int:
        """Index into `vis` of the segment at output sample o (clamped to the timeline)."""
        if not self.vis:
            return -1
        o = min(max(o, 0), max(0, self.total - 1))
        return max(0, bisect.bisect_right(self._vstarts, o) - 1)

    def seg_at(self, o: float) -> Seg | None:
        i = self.seg_index(o)
        return self.vis[i] if i >= 0 else None

    def out_to_src(self, o: float) -> float:
        """Source sample shown at output sample o (an insert maps to where it sits)."""
        s = self.seg_at(o)
        if s is None:
            return 0.0
        off = min(max(o - s.out0, 0), s.n)
        if s.ins:
            return float(s.home)
        if s.kind == "audio":
            return float(s.src0 + off)
        return s.src0 + (s.src1 - s.src0) * (off / s.n if s.n else 0.0)

    def src_to_out(self, src: float) -> tuple[float, bool]:
        """Output sample showing source sample `src`, and whether that spot was cut out
        (then the join where it would have been)."""
        if not self._srcsegs:
            return 0.0, True
        j = bisect.bisect_right(self._srcstarts, src) - 1
        if j < 0:
            return float(self._srcsegs[0].out0), True
        s = self._srcsegs[j]
        if src <= s.src1:
            if s.kind == "audio":
                return float(s.out0 + (src - s.src0)), False
            span = s.src1 - s.src0
            return s.out0 + (s.n * (src - s.src0) / span if span else 0.0), False
        if j + 1 < len(self._srcsegs):
            return float(self._srcsegs[j + 1].out0), True
        return float(self.total), True

    def portions(self, a: float, b: float, hidden: bool = False) -> list[tuple[Seg, int, int]]:
        """Visible pieces of [a, b): (seg, offset from, offset to) inside that seg. With `hidden`,
        also the tails heard under a crossfade whose join lies in the selection (deleting across a
        join must take them too)."""
        a, b = int(round(max(0, a))), int(round(min(b, self.total)))
        out = []
        if b <= a or not self.vis:
            return out
        i = max(0, bisect.bisect_right(self._vstarts, a) - 1)
        while i < len(self.vis) and self.vis[i].out0 < b:
            s = self.vis[i]
            lo, hi = max(a, s.out0), min(b, s.end)
            if hi > lo:
                out.append((s, lo - s.out0, hi - s.out0))
            i += 1
        if hidden:
            for s in self.hidden:
                if a <= s.out0 < b:
                    out.append((s, 0, min(b, s.end) - s.out0))
        return out

    # ------------------------------------------------------------------ waveform

    def block_peaks(self, src_min: np.ndarray, src_max: np.ndarray, block: int,
                    src_ms: np.ndarray | None = None):
        """Per-block min/max of the output (blocks of `block` samples) from per-block source peaks;
        with `src_ms` (per-block mean square) also the output's per-block mean square.
        Approximate to ±1 block at segment edges: it is for drawing, not for sound."""
        nb = -(-self.total // block) if self.total else 0
        omin = np.zeros(nb, dtype=np.float32)
        omax = np.zeros(nb, dtype=np.float32)
        oms = np.zeros(nb, dtype=np.float32) if src_ms is not None else None
        ns = len(src_min)
        for s in self.vis:
            if s.kind != "audio" or s.n <= 0:
                continue
            b0, b1 = s.out0 // block, -(-s.end // block)
            sb0 = s.src0 // block
            cnt = min(b1 - b0, ns - sb0)
            if cnt <= 0:
                continue
            lo, hi = src_min[sb0:sb0 + cnt], src_max[sb0:sb0 + cnt]
            ms = src_ms[sb0:sb0 + cnt] if oms is not None else None
            g = self._gain(s, 0, s.n)
            if g is not None:
                if not np.isscalar(g):
                    idx = np.minimum((np.arange(cnt) * block + block // 2), s.n - 1)
                    g = g[idx]
                lo, hi = lo * g, hi * g
                if ms is not None:
                    ms = ms * (g * g)
            np.minimum(omin[b0:b0 + cnt], lo, out=omin[b0:b0 + cnt])
            np.maximum(omax[b0:b0 + cnt], hi, out=omax[b0:b0 + cnt])
            if ms is not None:
                np.maximum(oms[b0:b0 + cnt], ms, out=oms[b0:b0 + cnt])
        return (omin, omax) if oms is None else (omin, omax, oms)


# ===================================================================== the edit list

@dataclass
class EditList:
    duration: float
    sample_rate: int
    cuts: list[Cut] = field(default_factory=list)
    pauses: list[Pause] = field(default_factory=list)
    crossfade_s: float = 0.300          # equal-power crossfade at cut joins
    pause_edge_s: float = 0.030         # fade length into/out of digital-silence pauses
    silence_pauses: bool = True         # pauses become digital silence (Sam's edit style)
    inserts: list[Insert] = field(default_factory=list)
    gains: list[Gain] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    source: str = DEFAULT_SOURCE        # the audio file (in the take folder) these times refer to

    # ------------------------------------------------------------------ queries

    def _active_cuts_x(self) -> list[tuple[float, float, float]]:
        """Merged, applied cut ranges clipped to the take, with the crossfade at each join."""
        rs = sorted((max(0.0, c.start), min(self.duration, c.end),
                     self.crossfade_s if c.xfade_s is None else c.xfade_s) for c in self.cuts if not c.keep)
        out: list[list[float]] = []
        for s, e, xf in rs:
            if e <= s:
                continue
            if out and s <= out[-1][1]:
                out[-1][1] = max(out[-1][1], e)
                out[-1][2] = min(out[-1][2], xf)
            else:
                out.append([s, e, xf])
        return [(s, e, xf) for s, e, xf in out]

    def active_cuts(self) -> list[tuple[float, float]]:
        """Merged, applied cut ranges, clipped to the take."""
        return [(s, e) for s, e, _ in self._active_cuts_x()]

    def kept_ranges(self) -> list[tuple[float, float]]:
        out, t = [], 0.0
        for s, e in self.active_cuts():
            if s > t:
                out.append((t, s))
            t = e
        if t < self.duration:
            out.append((t, self.duration))
        return out

    def output_duration(self) -> float:
        return self.timeline().duration

    def summary(self) -> dict:
        by: dict[str, list[Cut]] = {}
        for c in self.cuts:
            by.setdefault(c.reason, []).append(c)
        return {
            "cuts": {r: {"count": len(cs), "removed_s": round(sum(c.length for c in cs if not c.keep), 1),
                         "kept_anyway": sum(c.keep for c in cs)} for r, cs in by.items()},
            "pauses": len(self.pauses),
            "markers": sum(p.kind == "marker" for p in self.pauses),
            "inserts": len(self.inserts),
            "gains": len(self.gains),
            "source_s": round(float(self.duration), 1),
            "output_s": round(float(self.output_duration()), 1),
        }

    # ------------------------------------------------------------------ timeline

    def timeline(self) -> Timeline:
        sr = self.sample_rate
        n_src = int(round(self.duration * sr))
        ef = int(round(self.pause_edge_s * sr))
        ie = int(round(INSERT_EDGE_S * sr))
        cuts = self._active_cuts_x()
        kept: list[tuple[float, float]] = []
        joins: list[float] = []            # crossfade (s) between kept[k] and kept[k+1]
        t = 0.0
        for s, e, xf in cuts:
            if s > t:
                if kept:
                    joins.append(pending_xf)
                kept.append((t, s))
            pending_xf = xf
            t = e
        if t < self.duration:
            if kept:
                joins.append(pending_xf)
            kept.append((t, self.duration))
        if not kept and self.inserts:
            kept = [(0.0, 0.0)]

        # pauses, clipped to the kept ranges; a pause losing part of its source to a cut also
        # loses that much of its output length (shared between its parts)
        parts: list[list[tuple[int, int, int, Pause]]] = [[] for _ in kept]
        for p in self.pauses:
            if not self.silence_pauses and abs(p.new_length - p.length) < 1e-6:
                continue                     # untouched natural pause
            clipped = [(k, max(p.start, A), min(p.end, B)) for k, (A, B) in enumerate(kept)
                       if min(p.end, B) > max(p.start, A)]
            if not clipped:
                continue
            total = sum(e - s for _, s, e in clipped)
            removed = p.length - total
            target = p.new_length if removed < 1e-9 else max(0.0, p.new_length - removed)
            for k, s, e in clipped:
                if e - s <= 2 * self.pause_edge_s:
                    continue
                n_new = int(round(target * (e - s) / total * sr)) if removed >= 1e-9 else int(round(p.new_length * sr))
                parts[k].append((int(round(s * sr)), int(round(e * sr)), n_new, p))
        for lst in parts:
            lst.sort(key=lambda q: q[0])

        # inserts: placed in the kept range holding their time (inside a cut → just after it)
        homes: list[list[tuple[int, Insert]]] = [[] for _ in kept]
        for ins in self.inserts:
            k, at = _home(kept, ins.at)
            if k is not None:
                homes[k].append((int(round(at * sr)), ins))

        segs: list[Seg] = []
        pieces: list[Piece] = []
        for k, (A, B) in enumerate(kept):
            local: list[Seg] = []
            pos = 0

            def add(kind, n, src0, src1, **kw):
                nonlocal pos
                if n <= 0:
                    return
                local.append(Seg(pos, n, kind, src0, src1, piece=len(pieces), **kw))
                pos += n

            def put_insert(P: int, ins: Insert):
                loc = 0
                for ca, cb in ins.clips:
                    if ca is None:
                        n = int(round(cb * sr))
                        add("silence", n, P, P, owner=ins.id, ins=True, home=P, local0=loc)
                    else:
                        s0 = min(max(int(round(ca * sr)), 0), n_src)
                        s1 = min(max(int(round(cb * sr)), s0), n_src)
                        n = s1 - s0
                        add("audio", n, s0, s1, owner=ins.id, ins=True, home=P, local0=loc,
                            fin=min(ie, n), fout=min(ie, n))
                    loc += n

            def audio_run(s0: int, s1: int, fin: int, fout: int, pts):
                cur, cfin = s0, fin
                for P, ins in pts:
                    if P > cur:
                        add("audio", P - cur, cur, P, fin=min(cfin, P - cur), fout=min(ie, P - cur))
                    put_insert(P, ins)
                    cur, cfin = P, ie
                if s1 > cur:
                    add("audio", s1 - cur, cur, s1, fin=min(cfin, s1 - cur), fout=min(fout, s1 - cur))

            ia, ib = int(round(A * sr)), int(round(B * sr))
            pts = sorted(homes[k], key=lambda q: q[0])   # stable: list order at equal times
            cur, cfin = ia, 0
            prev_end = ia
            for ps, pe, n_new, p in parts[k]:
                if ps < prev_end:
                    continue                 # overlapping pauses: the first one wins
                q0 = ps + ef
                q1 = max(q0, pe - ef)
                before = [(P, i) for P, i in pts if P <= ps]
                inside = [(min(max(P, q0), q1), i) for P, i in pts if ps < P < pe]
                pts = [(P, i) for P, i in pts if P >= pe]
                audio_run(cur, q0, cfin, ef, before)
                nsil = max(0, n_new - 2 * ef)
                used, cq = 0, q0
                for P, ins in inside:
                    kk = int(round(nsil * (P - q0) / (q1 - q0))) if q1 > q0 else 0
                    add("silence", kk - used, cq, P, owner=p.id)
                    used, cq = kk, P
                    put_insert(P, ins)
                add("silence", nsil - used, cq, q1, owner=p.id)
                cur, cfin, prev_end = q1, ef, pe
            audio_run(cur, ib, cfin, 0, pts)
            if pos <= 0:
                continue
            if pieces:
                xf_s = joins[k - 1] if k - 1 < len(joins) else self.crossfade_s
                prev = pieces[-1]
                xf = min(int(round(xf_s * sr)), prev.n, pos)
                prev.xf_out = xf
                piece = Piece(prev.end - xf, pos, xf_in=xf)
            else:
                piece = Piece(0, pos)
            for s in local:
                s.out0 += piece.out0
                s.piece = len(pieces)
            pieces.append(piece)
            segs.extend(local)
        return Timeline(sr, segs, pieces, self.gains)

    # ------------------------------------------------------------------ render

    def render(self, x: np.ndarray) -> np.ndarray:
        """Apply every edit to the source samples (cuts with crossfades, pause shaping, inserts,
        gains and fades)."""
        sr = self.sample_rate
        if abs(len(x) / sr - self.duration) > 0.05:
            raise ValueError(f"audio is {len(x)/sr:.2f}s but the edit list is for {self.duration:.2f}s")
        x = np.asarray(x, dtype=np.float32)
        return self.timeline().render(x)

    # ------------------------------------------------------------------ persistence

    def to_dict(self) -> dict:
        """Plain copy (same as dataclasses.asdict, ~20× faster: the editor snapshots it on every edit)."""
        d = dict(self.__dict__)
        d["cuts"] = [dict(c.__dict__) for c in self.cuts]
        d["pauses"] = [dict(p.__dict__) for p in self.pauses]
        d["inserts"] = [{**i.__dict__, "clips": [list(c) for c in i.clips]} for i in self.inserts]
        d["gains"] = [dict(g.__dict__) for g in self.gains]
        d["markers"] = [dict(m.__dict__) for m in self.markers]
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "EditList":
        d = dict(d)
        d["cuts"] = [Cut(**c) for c in d.get("cuts", [])]
        d["pauses"] = [Pause(**p) for p in d.get("pauses", [])]
        d["inserts"] = [Insert(**{**i, "clips": [list(c) for c in i.get("clips", [])]}) for i in d.get("inserts", [])]
        d["gains"] = [Gain(**g) for g in d.get("gains", [])]
        d["markers"] = [Marker(**m) for m in d.get("markers", [])]
        return cls(**d)

    @classmethod
    def from_json(cls, s: str) -> "EditList":
        return cls.from_dict(json.loads(s))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "EditList":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


def _home(kept: list[tuple[float, float]], at: float) -> tuple[int | None, float]:
    """Kept range an insert at source time `at` belongs to (inside a cut → the start of the next one)."""
    for k, (A, B) in enumerate(kept):
        if A - 1e-9 <= at <= B + 1e-9:
            return k, min(max(at, A), B)
    for k, (A, B) in enumerate(kept):
        if A > at:
            return k, A
    if kept:
        return len(kept) - 1, kept[-1][1]
    return None, at
