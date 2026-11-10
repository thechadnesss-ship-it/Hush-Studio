"""Quiet-time forecast (CLAUDE.md Module G): when is the sky over Sam's place likely to be quiet?

* Weekday × hour heatmap of expected loud passes per hour = passes / hours actually observed.
* Runway-aware: FACT runway 01 vs 19 changes which aircraft pass over Parow. When the current
  mode is known and there's enough history for it, the forecast uses that mode's numbers.
* Cold start: a generic airport day (morning and evening peaks, quiet late night), blended out
  as personal history builds up ("learning your sky", ~7 days).
* Best windows today: the top 3 non-overlapping windows (default 60 min) with the fewest
  expected planes, from now until midnight.

Honest framing: it's a forecast, not a promise. The guard still runs while she records.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

# Expected loud passes per hour over a home near a busy runway, by local hour (generic shape:
# quiet small hours, morning bank 06–09, steady day, evening bank 17–20). Relative guide only.
GENERIC_RATE = np.array([0.3, 0.2, 0.1, 0.1, 0.2, 0.8, 3.0, 4.5, 4.5, 3.5, 2.5, 2.5,
                         2.5, 2.5, 2.5, 3.0, 3.5, 4.5, 4.5, 4.0, 3.0, 2.0, 1.2, 0.6])
LEARN_DAYS = 7.0
MIN_BUCKET_H = 0.5          # observed time needed before a weekday×hour cell is trusted


@dataclass
class Forecast:
    rate: np.ndarray                      # [7 weekdays (Mon=0), 24 hours] expected loud passes/hour
    observed_hours: float
    observed_days: float
    personal_weight: float
    runway_mode: str | None
    passes_logged: int

    @property
    def learning(self) -> bool:
        return self.personal_weight < 1.0

    def learning_label(self) -> str:
        if not self.learning:
            return ""
        return f"learning your sky (day {min(int(self.observed_days) + 1, int(LEARN_DAYS))} of {int(LEARN_DAYS)})"

    def expected(self, start: datetime, end: datetime) -> float:
        """Expected loud passes between two local datetimes."""
        total, t = 0.0, start
        while t < end:
            nxt = min(end, (t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)))
            total += self.rate[t.weekday(), t.hour] * (nxt - t).total_seconds() / 3600
            t = nxt
        return total


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    expected: float

    def label(self) -> str:
        n = self.expected
        planes = "~0" if n < 0.5 else f"~{n:.0f} plane{'s' if round(n) != 1 else ''}"
        return f"{self.start:%H:%M}–{self.end:%H:%M} ({planes})"


def build(passes: list[tuple], polls: list[tuple], runway_mode: str | None = None,
          loud_db: float = 60.0, tz_offset_s: float | None = None) -> Forecast:
    """`passes` rows: (ts, level_db, runway_mode, ...); `polls` rows: (ts, interval_s, runway_mode)."""
    def local(ts: float) -> datetime:
        return datetime.fromtimestamp(ts) if tz_offset_s is None else datetime.fromtimestamp(ts + tz_offset_s, timezone.utc).replace(tzinfo=None)

    cov = {None: np.zeros((7, 24)), "01": np.zeros((7, 24)), "19": np.zeros((7, 24))}
    cnt = {None: np.zeros((7, 24)), "01": np.zeros((7, 24)), "19": np.zeros((7, 24))}
    days = set()
    polls = sorted(polls)
    for i, (ts, interval, mode) in enumerate(polls):
        d = local(ts)
        gap = polls[i + 1][0] - ts if i + 1 < len(polls) else float(interval or 60.0)
        # each poll covers until the next one (overlapping loggers don't double count), and a
        # gap in polling isn't observed time
        h = min(float(interval or 60.0), gap, 300.0) / 3600.0
        cov[None][d.weekday(), d.hour] += h
        if mode in ("01", "19"):
            cov[mode][d.weekday(), d.hour] += h
        days.add(d.date())
    n_pass = 0
    for row in passes:
        ts, level, mode = row[0], row[1], row[2]
        if level is None or level < loud_db:
            continue
        n_pass += 1
        d = local(ts)
        cnt[None][d.weekday(), d.hour] += 1
        if mode in ("01", "19"):
            cnt[mode][d.weekday(), d.hour] += 1

    observed_h = float(cov[None].sum())
    observed_days = observed_h / 24.0
    w = min(1.0, observed_days / LEARN_DAYS)

    def rates(c, k):
        r = np.full((7, 24), np.nan)
        ok = k >= MIN_BUCKET_H
        r[ok] = c[ok] / k[ok]
        # unobserved weekday-hours: same hour on other days, else generic
        hour_cov, hour_cnt = k.sum(0), c.sum(0)
        by_hour = np.where(hour_cov >= MIN_BUCKET_H, hour_cnt / np.maximum(hour_cov, 1e-9), GENERIC_RATE)
        return np.where(np.isnan(r), by_hour[None, :], r)

    personal = rates(cnt[None], cov[None])
    if runway_mode in ("01", "19") and cov[runway_mode].sum() >= 24:   # ≥ a day of this mode
        mode_rates = rates(cnt[runway_mode], cov[runway_mode])
        personal = np.where(cov[runway_mode] >= MIN_BUCKET_H, mode_rates, personal)
    rate = w * personal + (1 - w) * GENERIC_RATE[None, :]
    return Forecast(rate, observed_h, observed_days, w, runway_mode, n_pass)


def best_windows(fc: Forecast, now: datetime | None = None, length_min: int = 60, step_min: int = 10,
                 n: int = 3, until: datetime | None = None) -> list[Window]:
    """Top-n non-overlapping windows with the fewest expected planes, from now until midnight."""
    now = now or datetime.now()
    start = now.replace(second=0, microsecond=0)
    start += timedelta(minutes=(-start.minute) % step_min)
    until = until or (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
    cands = []
    t = start
    while t + timedelta(minutes=length_min) <= until:
        cands.append(Window(t, t + timedelta(minutes=length_min), fc.expected(t, t + timedelta(minutes=length_min))))
        t += timedelta(minutes=step_min)
    chosen: list[Window] = []
    for w in sorted(cands, key=lambda w: (round(w.expected, 2), w.start)):
        if all(w.end <= c.start or w.start >= c.end for c in chosen):
            chosen.append(w)
        if len(chosen) == n:
            break
    return sorted(chosen, key=lambda w: w.start)


def summary_line(fc: Forecast, now: datetime | None = None) -> str:
    wins = best_windows(fc, now)
    if not wins:
        return "No full quiet hour left today. Tomorrow's forecast is ready after midnight."
    text = "Quietest today: " + ", ".join(w.label() for w in wins)
    if fc.learning:
        text += f" · {fc.learning_label()}"
    return text
