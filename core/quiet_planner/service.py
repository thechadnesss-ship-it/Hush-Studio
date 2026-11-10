"""Background sky logging for the planner.

`PlannerService` polls the radar every 60 s while the app is open and idle. While recording,
the recorder's own 5 s radar snapshots are fed in instead (no double polling). The headless
`planner_logger.py` script does the same without the app, so history can build up all day.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from ..plane_guard.sky_radar import Home, RadarSnapshot, SkyRadar
from .forecast import Forecast, build
from .logger import DB_PATH, SkyLogger


class PlannerService:
    def __init__(self, db_path: Path = DB_PATH, home: Home = Home(), radar: SkyRadar | None = None,
                 interval_s: float = 60.0):
        self.logger = SkyLogger(db_path, home)
        self.radar = radar or SkyRadar(home)
        self.interval_s = interval_s
        self.recording = False
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        self._t = threading.Thread(target=self._loop, name="sky-logger", daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        if self._t:
            self._t.join(timeout=5)
        self.logger.close()

    def on_recorder_snapshot(self, snap: RadarSnapshot) -> None:
        """Called from the recorder's radar thread (5 s cadence)."""
        try:
            self.logger.record(snap, interval_s=5.0)
        except Exception as e:
            self.last_error = str(e)

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self.recording:
                try:
                    snap = self.radar.poll()
                    self.logger.record(snap, interval_s=self.interval_s)
                    self.last_error = snap.error
                except Exception as e:
                    self.last_error = str(e)
            self._stop.wait(self.interval_s)

    def forecast(self, days: int = 42) -> Forecast:
        since = time.time() - days * 86400
        return build(self.logger.passes(since), self.logger.polls(since), self.logger.current_mode())
