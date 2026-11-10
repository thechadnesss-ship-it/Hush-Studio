"""Overflight logger (CLAUDE.md Module G): learns when Sam's sky is quiet.

Every radar snapshot (60 s while the app is idle, 5 s while recording) is logged:
  polls  : one row per snapshot → which hours were actually observed (coverage)
  passes : one row per aircraft pass that comes within 3 km horizontally and below 1 500 m,
           with closest distance, altitude, direction, runway mode and predicted loudness.

A pass is recorded once per aircraft per 20 min, at its predicted closest approach (so a 60 s
poll interval can't skip past it). Runway mode (FACT 01 vs 19) is inferred from low aircraft
near the airport and stored with every poll, so forecasts can split by it.
"""
from __future__ import annotations

import math
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import DATA_ROOT
from ..plane_guard.sky_radar import (FACT_RWY_01, Aircraft, Home, RadarSnapshot, _angle_diff,
                                     loudness_db, predict, to_local)

DB_PATH = DATA_ROOT / "data" / "sky.sqlite"
FACT = Home(-33.9648, 18.6017)            # airport reference point

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
    ts REAL NOT NULL, feed TEXT, n_aircraft INTEGER, runway_mode TEXT, interval_s REAL
);
CREATE INDEX IF NOT EXISTS polls_ts ON polls(ts);
CREATE TABLE IF NOT EXISTS passes (
    ts REAL NOT NULL, hex TEXT, callsign TEXT, type TEXT, category TEXT,
    alt_m REAL, closest_m REAL, direction TEXT, runway_mode TEXT, level_db REAL
);
CREATE INDEX IF NOT EXISTS passes_ts ON passes(ts);
"""


@dataclass(frozen=True)
class LoggerConfig:
    pass_radius_m: float = 3000.0
    pass_max_alt_m: float = 1500.0
    dedupe_s: float = 20 * 60
    runway_near_km: float = 15.0
    runway_max_alt_ft: float = 4000.0


def runway_mode(aircraft: list[Aircraft], cfg: LoggerConfig = LoggerConfig()) -> str | None:
    """'01' or '19' from low aircraft near FACT flying along the runway axis; None if unclear."""
    a, b = FACT_RWY_01.a, FACT_RWY_01.b
    ax, ay = to_local(Home(*a), *b)
    hdg01 = math.degrees(math.atan2(ax, ay)) % 360            # ≈ 346°
    votes = {"01": 0, "19": 0}
    for ac in aircraft:
        if ac.gs_kt < 40 or ac.alt_ft > cfg.runway_max_alt_ft:
            continue
        x, y = to_local(FACT, ac.lat, ac.lon)
        if math.hypot(x, y) > cfg.runway_near_km * 1000:
            continue
        if _angle_diff(ac.track_deg, hdg01) < 30:
            votes["01"] += 1                                    # departing north / arriving from the south
        elif _angle_diff(ac.track_deg, (hdg01 + 180) % 360) < 30:
            votes["19"] += 1
    if votes["01"] == votes["19"]:
        return None
    return "01" if votes["01"] > votes["19"] else "19"


class SkyLogger:
    """Thread-safe: `record()` may be called from the recorder's radar thread and the idle poller."""

    def __init__(self, db_path: Path = DB_PATH, home: Home = Home(), cfg: LoggerConfig = LoggerConfig()):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.home = home.rounded()
        self.cfg = cfg
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._recent: dict[str, float] = {}           # hex → ts of last logged pass
        self.last_mode: str | None = None

    def close(self):
        with self._lock:
            self._db.close()

    def record(self, snap: RadarSnapshot, interval_s: float = 60.0, now: float | None = None) -> int:
        """Log one snapshot. Returns the number of new passes."""
        if snap.feed is None:
            return 0
        now = snap.t if now is None else now
        mode = runway_mode(snap.aircraft, self.cfg)
        if mode:
            self.last_mode = mode
        rows = []
        for ac in snap.aircraft:
            if ac.on_ground or ac.age_s > 60:
                continue
            t, d, alt = predict(ac, self.home)
            # the closest approach must fall before the next poll (or be now)
            if t > interval_s + 5 or d > self.cfg.pass_radius_m or alt > self.cfg.pass_max_alt_m:
                continue
            last = self._recent.get(ac.hex)
            if last is not None and now - last < self.cfg.dedupe_s:
                continue
            if self._logged_recently(ac.hex, now):          # another logger process got it
                self._recent[ac.hex] = now
                continue
            self._recent[ac.hex] = now
            direction = "arrival" if ac.vrate_fpm < -200 else ("departure" if ac.vrate_fpm > 200 else "level")
            rows.append((now + t, ac.hex, ac.callsign, ac.type, ac.category, alt, d, direction,
                         mode or self.last_mode, loudness_db(ac.category, d, alt)))
        with self._lock:
            self._db.execute("INSERT INTO polls VALUES (?,?,?,?,?)",
                             (now, snap.feed, len(snap.aircraft), mode or self.last_mode, interval_s))
            if rows:
                self._db.executemany("INSERT INTO passes VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            self._db.commit()
        self._recent = {h: ts for h, ts in self._recent.items() if now - ts < self.cfg.dedupe_s}
        return len(rows)

    def _logged_recently(self, hexid: str, now: float) -> bool:
        with self._lock:
            r = self._db.execute("SELECT 1 FROM passes WHERE hex = ? AND ts >= ? LIMIT 1",
                                 (hexid, now - self.cfg.dedupe_s)).fetchone()
        return r is not None

    # ------------------------------------------------------------------ reads (for forecast.py)

    def passes(self, since: float = 0.0) -> list[tuple]:
        with self._lock:
            return self._db.execute("SELECT ts, level_db, runway_mode, callsign, closest_m, alt_m FROM passes "
                                    "WHERE ts >= ? ORDER BY ts", (since,)).fetchall()

    def polls(self, since: float = 0.0) -> list[tuple]:
        with self._lock:
            return self._db.execute("SELECT ts, interval_s, runway_mode FROM polls WHERE ts >= ? ORDER BY ts",
                                    (since,)).fetchall()

    def current_mode(self, max_age_s: float = 3 * 3600) -> str | None:
        with self._lock:
            r = self._db.execute("SELECT runway_mode FROM polls WHERE runway_mode IS NOT NULL AND ts >= ? "
                                 "ORDER BY ts DESC LIMIT 1", (time.time() - max_age_s,)).fetchone()
        return r[0] if r else None
