"""Turns guard holds (RED → CLEAR) into clean, reviewable cuts in the edit list.

Cut start, from the noise *onset* (the ear's "rising" warning if it came shortly before RED,
otherwise RED itself; the ear under-reads rumble, so the plane is often already audible at
"rising"):
  * onset during a pause → the cut starts where she stopped talking. This also covers the
    radar AMBER case: she finished her sentence and waited, so RED lands in her pause.
  * onset mid-phrase → back to the end of the previous phrase (up to `lookback_s`, 8 s), so a
    sentence half-spoken under a rising plane is removed whole, not chopped mid-word.
Cut end = CLEAR; silence between CLEAR and her next word is trimmed to `max_gap_s`.
Cuts are applied (300 ms equal-power crossfades) by EditList.render(), so Sam can preview
each one and choose "Keep anyway".
"""
from __future__ import annotations

from dataclasses import dataclass

from ..edl import Cut, EditList


@dataclass(frozen=True)
class SplicerConfig:
    lookback_s: float = 8.0
    max_gap_s: float = 2.5
    fallback_lead_s: float = 1.0    # mid-phrase with no earlier pause in reach
    onset_window_s: float = 60.0    # a "rising" warning this long before RED counts as onset


def _in_phrase(t: float, speech: list[tuple[float, float]]) -> tuple[float, float] | None:
    return next(((s, e) for s, e in speech if s <= t < e), None)


def plan_cut(
    red_t: float,
    clear_t: float,
    speech: list[tuple[float, float]],
    duration: float,
    onset_t: float | None = None,
    cfg: SplicerConfig = SplicerConfig(),
    reason: str = "plane",
) -> Cut:
    """`speech` = VAD speech regions of the take (source time, sorted)."""
    onset = red_t
    if onset_t is not None and 0 <= red_t - onset_t <= cfg.onset_window_s:
        onset = onset_t

    phrase = _in_phrase(onset, speech)
    before = [e for s, e in speech if e <= onset]
    if phrase is None:
        start = before[-1] if before else 0.0                      # start of the current pause
    else:
        prev = [e for e in before if e >= onset - cfg.lookback_s]
        start = prev[-1] if prev else max(0.0, onset - cfg.fallback_lead_s)

    end = clear_t
    nxt = [s for s, _ in speech if s >= clear_t]
    if nxt and nxt[0] - clear_t > cfg.max_gap_s:
        end = nxt[0] - cfg.max_gap_s
    ph = _in_phrase(end, speech)
    if ph is not None and ph[0] < end:                             # never cut into a phrase
        end = ph[0]
    end = min(end, duration)
    if end <= start:
        end = min(duration, start + 0.01)
    return Cut(start=round(start, 3), end=round(end, 3), reason=reason)


def add_plane_cuts(edl: EditList, holds: list[tuple[float, float]], speech: list[tuple[float, float]],
                   onsets: list[float] | None = None, cfg: SplicerConfig = SplicerConfig()) -> list[Cut]:
    """`onsets` = times of the ear's "rising" warnings."""
    onsets = sorted(onsets or [])
    new = []
    for red, clear in holds:
        onset = max((o for o in onsets if 0 <= red - o <= cfg.onset_window_s), default=None)
        c = plan_cut(red, clear, speech, edl.duration, onset, cfg)
        edl.cuts.append(c)
        new.append(c)
    return new
