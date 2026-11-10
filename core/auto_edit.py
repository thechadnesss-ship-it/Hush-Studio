"""Auto-editor: does Sam's manual edit automatically, as reviewable Pause entries in the edit list.

Learned by matching her raw Riverside take against her hand-edited mp3 (2026-11-09):

* **Pause markers.** She says the same short word (~1.1 s, full speaking level) wherever a long
  silence should go: ~90 times in a 33 min take. Her edit removes each one and the pauses around
  it become ≈ 10 s of silence (9–13 s, median ≈ 10). These marker utterances are 0.86 similar to
  each other vs 0.42 for ordinary speech of the same length, so the app finds them itself, with
  no stored template: the one short, isolated utterance that repeats dozens of times nearly
  identically. (A template Sam confirms once can be passed in for later sessions.)
* **Ordinary pauses keep their length** (> 90% within ±0.5 s) but become digital silence.
* Leading silence trimmed to ~0.1 s; trailing silence kept up to ~13 s; extreme gaps capped.

Every decision is an entry Sam can change in review (marker ↔ not a marker, any pause length).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import signal

from .edl import Pause


@dataclass(frozen=True)
class AutoEditConfig:
    silence_pauses: bool = True
    marker_detect: bool = True
    marker_pause_s: float = 10.0       # her edit: 9–13 s, median ≈ 10
    induction_marker_pause_s: float | None = 5.0  # shorter pauses in the induction
    induction_end_s: float = 720.0                # early marker phase ends ~12 min in
    induction_min_start_s: float = 180.0          # avoid changing short takes/tests
    marker_len_s: tuple[float, float] = (0.6, 1.8)
    marker_sim: float = 0.80           # detection threshold vs the marker template
    cluster_sim: float = 0.85          # "near-identical" for discovering the marker
    cluster_min: int = 8               # it must repeat at least this often to count as a marker
    cap_pause_s: float = 15.0          # her edit: only 1 pause over 15 s (raw had 7)
    lead_s: float = 0.1
    tail_s: float = 13.0


# ----------------------------------------------------------------------------- marker features

_FEAT_SR = 16000


def phrase_features(x: np.ndarray, sr: int, phrases: list[tuple[float, float]]) -> np.ndarray:
    """Fixed-size spectral shape of each phrase: 18 log bands (250–5000 Hz) × 30 time steps,
    mean-removed and unit-norm, so similarity ignores level and small timing differences."""
    y = x if sr == _FEAT_SR else signal.resample_poly(x, _FEAT_SR, sr).astype(np.float32)
    edges = np.geomspace(250, 5000, 19)
    out = []
    for s, e in phrases:
        seg = y[int(s * _FEAT_SR): int(e * _FEAT_SR)]
        f, _, S = signal.stft(seg, _FEAT_SR, nperseg=400, noverlap=240)
        S = np.log(np.abs(S) + 1e-6)
        B = np.stack([S[(f >= a) & (f < b)].mean(0) for a, b in zip(edges, edges[1:])])
        B = signal.resample(B, 30, axis=1)
        B = B - B.mean()
        out.append((B / (np.linalg.norm(B) + 1e-9)).ravel())
    return np.array(out, dtype=np.float32).reshape(len(phrases), -1)


def find_markers(x: np.ndarray, sr: int, speech: list[tuple[float, float]], cfg: AutoEditConfig = AutoEditConfig(),
                 template: np.ndarray | None = None) -> tuple[list[int], np.ndarray | None]:
    """Indices into `speech` of pause-marker utterances, plus the template used."""
    lo, hi = cfg.marker_len_s
    cand = [i for i, (s, e) in enumerate(speech)
            if lo <= e - s <= hi and 0 < i < len(speech) - 1]          # isolated: speech on both sides
    if not cand:
        return [], template
    F = phrase_features(x, sr, [speech[i] for i in cand])
    if template is None:
        S = F @ F.T
        neighbours = (S >= cfg.cluster_sim).sum(axis=1) - 1
        core = np.flatnonzero(neighbours >= cfg.cluster_min)
        if core.size < cfg.cluster_min:
            return [], None                                          # nothing repeats: no markers
        seed = int(core[np.argmax(neighbours[core])])
        members = np.flatnonzero(S[seed] >= cfg.cluster_sim)
        template = F[members].mean(axis=0)
        template /= np.linalg.norm(template) + 1e-9
    sims = F @ template
    return [cand[j] for j in np.flatnonzero(sims >= cfg.marker_sim)], template


# ----------------------------------------------------------------------------- pause shaping

def shape_pauses(speech: list[tuple[float, float]], duration: float, cfg: AutoEditConfig = AutoEditConfig(),
                 cuts: list[tuple[float, float]] | None = None, markers: list[int] | None = None) -> list[Pause]:
    """Pause entries for the edit list. `markers` = indices into `speech` to remove."""
    cuts = cuts or []
    mset = set(markers or [])
    kept = [p for i, p in enumerate(speech) if i not in mset]
    marker_spans = [speech[i] for i in sorted(mset)]
    has_induction_phase = bool(marker_spans) and marker_spans[0][0] >= cfg.induction_min_start_s

    def overlaps_cut(a: float, b: float) -> bool:
        return any(a < ce and cs < b for cs, ce in cuts)

    out: list[Pause] = []
    if not kept:
        return out
    if kept[0][0] > cfg.lead_s:
        out.append(Pause(0.0, kept[0][0], cfg.lead_s, kind="lead"))
    for (_, a_end), (b_start, _) in zip(kept, kept[1:]):
        if b_start - a_end <= 0 or overlaps_cut(a_end, b_start):
            continue
        has_marker = any(a_end <= ms and me <= b_start for ms, me in marker_spans)
        length = b_start - a_end
        if has_marker:
            marker_start = min(ms for ms, me in marker_spans if a_end <= ms and me <= b_start)
            marker_pause_s = cfg.marker_pause_s
            if (has_induction_phase and cfg.induction_marker_pause_s is not None
                    and cfg.induction_min_start_s <= marker_start < cfg.induction_end_s):
                marker_pause_s = cfg.induction_marker_pause_s
            out.append(Pause(a_end, b_start, marker_pause_s, kind="marker"))
        elif length > cfg.cap_pause_s:
            out.append(Pause(a_end, b_start, cfg.cap_pause_s))
        elif cfg.silence_pauses:
            out.append(Pause(a_end, b_start, length))
    tail = duration - kept[-1][1]
    if tail > 0:
        out.append(Pause(kept[-1][1], duration, min(tail, cfg.tail_s), kind="tail"))
    return out


def auto_edit(x: np.ndarray, sr: int, speech: list[tuple[float, float]], duration: float,
              cfg: AutoEditConfig = AutoEditConfig(), cuts: list[tuple[float, float]] | None = None,
              template: np.ndarray | None = None) -> tuple[list[Pause], np.ndarray | None]:
    markers, template = find_markers(x, sr, speech, cfg, template) if cfg.marker_detect else ([], None)
    return shape_pauses(speech, duration, cfg, cuts, markers), template
