"""Hand-editing on top of the edit list: the model behind the waveform editor (T12), with undo/redo.

Sam edits what she hears, so every operation takes *output* times (seconds of the edited
session, as the waveform shows it) and turns them into source-time entries in the EditList:

    delete / cut     → Cut(reason="manual", short crossfade), or a shorter pause, or a trimmed paste
    silence          → Pause(kind="silence", locked) of exactly the same length (nothing moves)
    insert silence   → a longer pause, or Insert([None, seconds])
    copy / paste     → Insert(clips=[source ranges / silences])
    fade in / out    → Gain(fade=...)
    normalise        → Gain(db, reason="normalise")
    markers          → Marker
    auto-edit pauses / remove plane noise → the automatic detectors, on all or part of the take

Nothing writes to an audio file; raw.flac and clean.wav are never touched. Undo/redo keep
whole snapshots of the edit list (plain dicts, a few tens of KB each), so every step is exactly reversible.
The automated features are split into `compute_*` (slow, safe on a worker thread: they only read)
and `Editor.apply_*` (quick, on the GUI thread, one undo step each).
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace

import numpy as np

from .edl import DEFAULT_SOURCE, Cut, EditList, Gain, Insert, Marker, Pause, Timeline

MANUAL_XFADE_S = 0.010          # Sam's own cuts: a short crossfade, so what she sees is what she gets
MAX_UNDO = 300


# ===================================================================== clip lists (inserts)

def _clip_n(c, sr: int) -> int:
    a, b = c
    return int(round(b * sr)) if a is None else int(round(b * sr)) - int(round(a * sr))


def clips_length(clips, sr: int) -> int:
    return sum(_clip_n(c, sr) for c in clips)


def clips_slice(clips, l0: int, l1: int, sr: int) -> list[list]:
    """Samples [l0, l1) of a clip list, as a clip list."""
    out, pos = [], 0
    for a, b in clips:
        n = _clip_n((a, b), sr)
        j0, j1 = max(l0 - pos, 0), min(l1 - pos, n)
        if j1 > j0:
            if a is None:
                out.append([None, (j1 - j0) / sr])
            else:
                s0 = int(round(a * sr))
                out.append([(s0 + j0) / sr, (s0 + j1) / sr])
        pos += n
    return out


def clips_norm(clips, sr: int) -> list[list]:
    """Drop empty clips, join neighbours that continue each other."""
    out: list[list] = []
    for a, b in clips:
        if _clip_n((a, b), sr) <= 0:
            continue
        if out and a is None and out[-1][0] is None:
            out[-1][1] += b
        elif out and a is not None and out[-1][0] is not None and abs(out[-1][1] - a) * sr < 0.5:
            out[-1][1] = b
        else:
            out.append([a, b])
    return out


# ===================================================================== automated features (worker-safe)

def compute_auto_pauses(audio: np.ndarray, edl: EditList, auto=None, speech=None) -> list[Pause]:
    """Pause shaping (spoken pause-markers → long silence, other pauses → digital silence) for the
    whole take; `Editor.apply_auto_pauses` keeps the part Sam asked for."""
    from .auto_edit import AutoEditConfig, auto_edit
    from .vad import detect_speech
    sr = edl.sample_rate
    speech = detect_speech(audio, sr) if speech is None else speech
    pauses, _ = auto_edit(audio, sr, speech, edl.duration, auto or AutoEditConfig(), cuts=edl.active_cuts())
    return pauses


def compute_plane_cuts(raw: np.ndarray, audio: np.ndarray, edl: EditList, sa: float, sb: float,
                       speech=None) -> list[Cut]:
    """Plane / loud-noise cuts in source range [sa, sb]: the guard's ear on the raw take, then the
    splicer (cuts start at her pause before the noise and never cut into a phrase)."""
    from .plane_guard.ear import Ear, holds_from_events
    from .plane_guard.splicer import add_plane_cuts
    from .vad import detect_speech
    sr = edl.sample_rate
    pre = min(sa, 30.0)                    # context so the ear knows the room before the range
    a0 = sa - pre
    ear = Ear(sr)
    ear.feed(raw[int(a0 * sr): int(sb * sr)])
    events = [replace(e, t=e.t + a0) for e in ear.events]
    holds = [(r, c) for r, c in holds_from_events(events, sb) if c > sa and r < sb]
    speech = detect_speech(audio, sr) if speech is None else speech
    tmp = EditList(edl.duration, sr)
    new = add_plane_cuts(tmp, holds, speech, onsets=[e.t for e in events if e.kind == "rising"])
    return [c for c in new if c.end > sa and c.start < sb]


def compute_normalise(tl: Timeline, audio: np.ndarray, a: float | None = None, b: float | None = None,
                      target_lufs: float = -21.0) -> float:
    """dB to apply. Whole take: to the channel's loudness (what mastering will do anyway, so the
    waveform shows the real level). A part: to the loudness of the whole take, so a quiet stretch
    (she leaned away from the mic) matches the rest; never pushing its peak above −1 dBFS."""
    from .master import integrated_lufs
    sr = tl.sr
    y = tl.render(audio)
    whole = integrated_lufs(y, sr)
    if not np.isfinite(whole):
        raise ValueError("The take is silent; there is nothing to normalise.")
    if a is None or b is None:
        return float(np.clip(target_lufs - whole, -20.0, 20.0))
    seg = y[int(a * sr): int(b * sr)]
    part = integrated_lufs(seg, sr)
    if not np.isfinite(part):
        raise ValueError("Select at least half a second with her voice in it to normalise.")
    db = whole - part
    peak = float(np.max(np.abs(seg))) if len(seg) else 0.0
    if peak > 0:
        db = min(db, -1.0 - 20 * np.log10(peak))
    return float(np.clip(db, -12.0, 12.0))


# ===================================================================== the editor model

@dataclass
class CutMark:
    cut: Cut
    a: float           # output seconds: the join (active cut) or the region (kept / proposed cut)
    b: float
    active: bool


class Editor:
    """Undoable edits of one take. `audio` is the file `edl.source` names (clean.wav normally)."""

    def __init__(self, edl: EditList, audio: np.ndarray):
        self.edl = edl
        self.audio = np.asarray(audio, dtype=np.float32)
        self.sources: dict[str, np.ndarray] = {edl.source: self.audio}
        self._undo: list[tuple[str, dict]] = []
        self._redo: list[tuple[str, dict]] = []
        self.clipboard: list[list] | None = None
        self.version = 0
        self.tl: Timeline = edl.timeline()

    # ------------------------------------------------------------------ basics

    @property
    def sr(self) -> int:
        return self.edl.sample_rate

    @property
    def duration(self) -> float:
        return self.tl.duration

    def _rebuild(self) -> None:
        self.tl = self.edl.timeline()
        self.version += 1

    def _restore(self, snap: dict) -> None:
        new = EditList.from_dict(snap)
        self.edl.__dict__.update(new.__dict__)          # same object: the main window keeps its reference
        self.audio = self.sources.get(self.edl.source, self.audio)
        self._rebuild()

    @contextmanager
    def change(self, label: str):
        """One undo step. Rolls back if the operation fails half-way."""
        before = self.edl.to_dict()
        try:
            yield
        except Exception:
            self._restore(before)
            raise
        if self.edl.to_dict() == before:
            self._rebuild()
            return
        self._undo.append((label, before))
        del self._undo[:-MAX_UNDO]
        self._redo.clear()
        self._rebuild()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo_label(self) -> str:
        return self._undo[-1][0] if self._undo else ""

    def redo_label(self) -> str:
        return self._redo[-1][0] if self._redo else ""

    def undo(self) -> str:
        if not self._undo:
            return ""
        label, snap = self._undo.pop()
        self._redo.append((label, self.edl.to_dict()))
        self._restore(snap)
        return label

    def redo(self) -> str:
        if not self._redo:
            return ""
        label, snap = self._redo.pop()
        self._undo.append((label, self.edl.to_dict()))
        self._restore(snap)
        return label

    def render(self, a: float | None = None, b: float | None = None) -> np.ndarray:
        """Edited audio, output seconds [a, b) (default: all of it)."""
        sr = self.sr
        return self.tl.render(self.audio, 0 if a is None else int(round(a * sr)),
                              None if b is None else int(round(b * sr)))

    def src_range(self, a: float | None, b: float | None) -> tuple[float, float]:
        """Source seconds under output [a, b) (None → the whole take)."""
        if a is None or b is None:
            return 0.0, self.edl.duration
        sr = self.sr
        return self.tl.out_to_src(a * sr) / sr, self.tl.out_to_src(b * sr) / sr

    def _insert(self, iid: str) -> Insert:
        return next(i for i in self.edl.inserts if i.id == iid)

    def _pause(self, pid: str) -> Pause:
        return next(p for p in self.edl.pauses if p.id == pid)

    # ------------------------------------------------------------------ manual edits

    def delete(self, a: float, b: float) -> None:
        with self.change("Delete"):
            self._delete(a, b)

    def cut(self, a: float, b: float) -> None:
        self.copy(a, b)
        with self.change("Cut"):
            self._delete(a, b)

    def copy(self, a: float, b: float) -> None:
        sr = self.sr
        clips = []
        for s, k0, k1 in self.tl.portions(a * sr, b * sr):
            if s.kind == "audio":
                clips.append([(s.src0 + k0) / sr, (s.src0 + k1) / sr])
            else:
                clips.append([None, (k1 - k0) / sr])
        self.clipboard = clips_norm(clips, sr) or None

    def paste(self, at: float, replace: tuple[float, float] | None = None) -> None:
        if not self.clipboard:
            return
        with self.change("Paste"):
            if replace is not None and replace[1] > replace[0]:
                self._delete(*replace)
                self._rebuild()
                at = replace[0]
            self._insert_clips(at, [list(c) for c in self.clipboard])

    def insert_silence(self, at: float, length: float) -> None:
        if length <= 0:
            return
        with self.change("Insert silence"):
            s = self.tl.seg_at(at * self.sr)
            if s is not None and not s.ins and s.kind == "silence" and s.owner:
                p = self._pause(s.owner)
                p.new_length += length
                p.locked = True
            else:
                self._insert_clips(at, [[None, float(length)]])

    def silence(self, a: float, b: float) -> None:
        with self.change("Silence"):
            self._silence(a, b)

    def fade(self, a: float, b: float, kind: str) -> None:
        if b <= a:
            return
        with self.change("Fade in" if kind == "in" else "Fade out"):
            self.edl.gains.append(self._gain_for(a, b, fade=kind, reason="fade"))

    def add_marker(self, at: float, label: str = "") -> Marker:
        with self.change("Add marker"):
            m = Marker(round(self.tl.out_to_src(at * self.sr) / self.sr, 4),
                       label or f"Marker {len(self.edl.markers) + 1}")
            self.edl.markers.append(m)
        return m

    def rename_marker(self, mid: str, label: str) -> None:
        with self.change("Rename marker"):
            for m in self.edl.markers:
                if m.id == mid:
                    m.label = label

    def remove_marker(self, mid: str) -> None:
        with self.change("Remove marker"):
            self.edl.markers = [m for m in self.edl.markers if m.id != mid]

    def markers(self) -> list[tuple[float, Marker]]:
        sr = self.sr
        return sorted(((self.tl.src_to_out(m.t * sr)[0] / sr, m) for m in self.edl.markers), key=lambda q: q[0])

    # ------------------------------------------------------------------ cuts from the detectors

    def cut_marks(self) -> list[CutMark]:
        """Where each listed cut shows on the waveform: active cuts as a join, cuts that are not
        applied (kept anyway, or proposals waiting for Sam) as the region they would remove."""
        sr, out = self.sr, []
        active = self.edl.active_cuts()
        for c in self.edl.cuts:
            if c.keep:
                a, _ = self.tl.src_to_out(c.start * sr)
                b, _ = self.tl.src_to_out(c.end * sr)
                if b > a:
                    out.append(CutMark(c, a / sr, b / sr, False))
            elif any(s <= c.start and c.end <= e for s, e in active):
                j, _ = self.tl.src_to_out(c.end * sr)
                out.append(CutMark(c, j / sr, j / sr, True))
        return out

    def decide_cut(self, cid: str, action: str) -> None:
        """"accept" / "undo" a proposal, "restore" an applied cut (keep anyway), "apply" a kept one,
        "remove" it from the list altogether."""
        labels = {"accept": "Accept cut", "undo": "Undo proposed cut", "restore": "Restore cut",
                  "apply": "Cut again", "remove": "Remove cut"}
        with self.change(labels[action]):
            c = next(c for c in self.edl.cuts if c.id == cid)
            if action == "accept":
                c.accept()
            elif action == "undo":
                c.undo()
            elif action == "restore":
                c.keep, c.proposed = True, False
            elif action == "apply":
                c.keep, c.proposed = False, False
            elif action == "remove":
                self.edl.cuts.remove(c)

    # ------------------------------------------------------------------ automated features

    def apply_auto_pauses(self, new: list[Pause], sa: float, sb: float) -> int:
        """Replace the automatic pauses inside source [sa, sb] with `new` (Sam's own pauses stay)."""
        with self.change("Auto-edit pauses"):
            keep = [p for p in self.edl.pauses if p.locked or p.start < sa - 1e-9 or p.end > sb + 1e-9]
            add = [p for p in new if p.start >= sa - 1e-9 and p.end <= sb + 1e-9
                   and not any(p.start < k.end and k.start < p.end for k in keep)]
            self.edl.pauses = sorted(keep + add, key=lambda p: p.start)
        return len(add)

    def apply_plane_cuts(self, new: list[Cut], sa: float, sb: float) -> int:
        """Replace the applied plane cuts inside source [sa, sb] with `new`; cuts Sam chose to keep stay kept."""
        with self.change("Remove plane noise"):
            kept = [c for c in self.edl.cuts if c.keep]
            self.edl.cuts = [c for c in self.edl.cuts
                             if not (c.reason == "plane" and not c.keep and c.start >= sa - 1e-9 and c.end <= sb + 1e-9)]
            add = [c for c in new if not any(c.start < k.end and k.start < c.end for k in kept)
                   and not any(abs(c.start - o.start) < 1e-3 and abs(c.end - o.end) < 1e-3 for o in self.edl.cuts)]
            self.edl.cuts.extend(add)
        return len(add)

    def apply_normalise(self, db: float, a: float | None = None, b: float | None = None) -> None:
        if abs(db) < 0.05:
            return
        with self.change("Normalise"):
            if a is None or b is None:
                whole = [g for g in self.edl.gains if g.reason == "normalise" and g.on is None
                         and g.start <= 1e-6 and g.end >= self.edl.duration - 1e-6]
                if whole:
                    whole[0].db += db
                else:
                    self.edl.gains.append(Gain(0.0, self.edl.duration, db, reason="normalise"))
                for ins in self.edl.inserts:
                    gi = [g for g in self.edl.gains if g.on == ins.id and g.reason == "normalise"
                          and g.start <= 1e-6 and g.end >= ins.length - 1e-6]
                    if gi:
                        gi[0].db += db
                    elif ins.length > 0:
                        self.edl.gains.append(Gain(0.0, ins.length, db, reason="normalise", on=ins.id))
            else:
                self.edl.gains.append(self._gain_for(a, b, db=db, reason="normalise"))

    def bring_back(self, audio: np.ndarray, source: str) -> None:
        """Audio Sam edited in Audacity (the whole edited take) becomes the new base. Every earlier
        edit is in that file already, so the list starts fresh; markers move with the audio."""
        sr = self.sr
        with self.change("Bring back from Audacity"):
            marks = []
            for m in self.edl.markers:
                o, gone = self.tl.src_to_out(m.t * sr)
                if not gone:
                    marks.append(Marker(round(o / sr, 4), m.label, m.id))
            self.sources[source] = np.asarray(audio, dtype=np.float32)
            fresh = EditList(duration=len(audio) / sr, sample_rate=sr, crossfade_s=self.edl.crossfade_s,
                             pause_edge_s=self.edl.pause_edge_s, silence_pauses=self.edl.silence_pauses,
                             markers=marks, source=source)
            self.edl.__dict__.update(fresh.__dict__)
            self.audio = self.sources[source]

    @property
    def on_original(self) -> bool:
        """Edits refer to clean.wav (so the raw take lines up with them: plane detection works)."""
        return self.edl.source == DEFAULT_SOURCE

    # ------------------------------------------------------------------ internals

    def _gain_for(self, a: float, b: float, **kw) -> Gain:
        sr = self.sr
        sa, sb = self.tl.seg_at(a * sr), self.tl.seg_at(b * sr - 1)
        if sa is not None and sa.ins and sb is not None and sb.ins and sa.owner == sb.owner:
            la = (sa.local0 + (a * sr - sa.out0)) / sr
            lb = (sb.local0 + (b * sr - sb.out0)) / sr
            return Gain(la, max(lb, la + 1 / sr), on=sa.owner, **kw)
        s0, s1 = self.src_range(a, b)
        return Gain(s0, max(s1, s0 + 1 / sr), **kw)

    def _insert_clips(self, at: float, clips: list[list]) -> None:
        sr = self.sr
        o = at * sr
        s = self.tl.seg_at(o)
        if s is None:
            self.edl.inserts.append(Insert(0.0, clips_norm(clips, sr)))
            return
        if s.ins:
            ins = self._insert(s.owner)
            l = int(round(s.local0 + min(max(o - s.out0, 0), s.n)))
            n_all = clips_length(ins.clips, sr)
            ins.clips = clips_norm(clips_slice(ins.clips, 0, l, sr) + clips + clips_slice(ins.clips, l, n_all, sr), sr)
            self._shift_insert_gains(ins.id, l / sr, clips_length(clips, sr) / sr)
            return
        src = self.tl.out_to_src(o)
        self.edl.inserts.append(Insert(round(src / sr, 6), clips_norm(clips, sr)))

    def _shift_insert_gains(self, iid: str, at: float, by: float) -> None:
        """Gains inside an insert move when audio is added (by > 0) or removed (by < 0) at `at`."""
        keep = []
        for g in self.edl.gains:
            if g.on != iid:
                keep.append(g)
                continue
            if by >= 0:
                if g.start >= at:
                    g.start += by
                    g.end += by
                elif g.end > at:
                    g.end += by
                keep.append(g)
            else:
                r0, r1 = at, at - by
                if g.start >= r0 and g.end <= r1:
                    continue
                g.start = g.start if g.start < r0 else max(r0, g.start + by)
                g.end = g.end if g.end <= r0 else max(r0, g.end + by)
                if g.end > g.start:
                    keep.append(g)
        self.edl.gains = keep

    def _delete(self, a: float, b: float) -> None:
        sr, tl = self.sr, self.tl
        ranges: list[list[int]] = []
        covered: dict[str, int] = {}
        ins_cut: dict[str, list[tuple[int, int]]] = {}
        for s, k0, k1 in tl.portions(a * sr, b * sr, hidden=True):
            if s.ins:
                ins_cut.setdefault(s.owner, []).append((s.local0 + k0, s.local0 + k1))
            elif s.kind == "audio":
                ranges.append([s.src0 + k0, s.src0 + k1])
            elif s.owner:
                covered[s.owner] = covered.get(s.owner, 0) + (k1 - k0)
        # a pause selected whole goes (its source span is cut); partly selected, it gets shorter
        for pid, n in covered.items():
            total = sum(v.n for v in tl.vis if v.owner == pid and not v.ins and v.kind == "silence")
            p = self._pause(pid)
            if n >= total:
                for v in tl.vis:
                    if v.owner == pid and not v.ins and v.kind == "silence" and v.src1 > v.src0:
                        ranges.append([v.src0, v.src1])
            else:
                p.new_length = max(2 * self.edl.pause_edge_s, p.new_length - n / sr)
                p.locked = True
        for iid, rs in ins_cut.items():
            ins = self._insert(iid)
            for l0, l1 in sorted(rs, reverse=True):
                ins.clips = clips_norm(clips_slice(ins.clips, 0, l0, sr) +
                                       clips_slice(ins.clips, l1, clips_length(ins.clips, sr), sr), sr)
                self._shift_insert_gains(iid, l0 / sr, -(l1 - l0) / sr)
            if not ins.clips:
                self.edl.inserts.remove(ins)
                self.edl.gains = [g for g in self.edl.gains if g.on != iid]
        merged: list[list[int]] = []
        for r in sorted(ranges):
            if merged and r[0] <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], r[1])
            else:
                merged.append(list(r))
        for s0, s1 in merged:
            if s1 > s0:
                self.edl.cuts.append(Cut(s0 / sr, s1 / sr, "manual", xfade_s=MANUAL_XFADE_S))
                self.edl.markers = [m for m in self.edl.markers if not (s0 / sr < m.t < s1 / sr)]

    def _silence(self, a: float, b: float) -> None:
        sr, tl = self.sr, self.tl
        ranges: list[list[int]] = []
        for s, k0, k1 in tl.portions(a * sr, b * sr, hidden=True):
            if s.ins:
                if s.kind == "audio":
                    ins = self._insert(s.owner)
                    l0, l1 = s.local0 + k0, s.local0 + k1
                    ins.clips = clips_norm(clips_slice(ins.clips, 0, l0, sr) + [[None, (l1 - l0) / sr]] +
                                           clips_slice(ins.clips, l1, clips_length(ins.clips, sr), sr), sr)
            elif s.kind == "audio":
                ranges.append([s.src0 + k0, s.src0 + k1])
        if not ranges:
            return
        # grow each stretch over the pauses it touches, so pauses never overlap
        spans = sorted(ranges)
        for _ in range(4):
            grown = []
            for u0, u1 in spans:
                for p in self.edl.pauses:
                    p0, p1 = int(round(p.start * sr)), int(round(p.end * sr))
                    if p0 < u1 and u0 < p1:
                        u0, u1 = min(u0, p0), max(u1, p1)
                grown.append([u0, u1])
            grown.sort()
            spans = []
            for u in grown:
                if spans and u[0] <= spans[-1][1] + 1:
                    spans[-1][1] = max(spans[-1][1], u[1])
                else:
                    spans.append(u)
        for u0, u1 in spans:
            o0, _ = tl.src_to_out(u0)
            o1, _ = tl.src_to_out(u1)
            ins_n = sum(v.n for v in tl.vis if v.ins and u0 < v.home <= u1)
            new_len = max(0.0, (o1 - o0 - ins_n) / sr)
            self.edl.pauses = [p for p in self.edl.pauses
                               if not (int(round(p.start * sr)) < u1 and u0 < int(round(p.end * sr)))]
            self.edl.pauses.append(Pause(u0 / sr, u1 / sr, round(new_len, 6), locked=True, kind="silence"))
        self.edl.pauses.sort(key=lambda p: p.start)
