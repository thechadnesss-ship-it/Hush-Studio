"""Quiet-time planner: pass logging, runway mode, forecast, best windows."""
import math
from datetime import datetime, timedelta

import numpy as np
import pytest

from core.plane_guard.sky_radar import FACT_RWY_01, Aircraft, Home, RadarSnapshot
from core.quiet_planner.forecast import GENERIC_RATE, best_windows, build, summary_line
from core.quiet_planner.logger import SkyLogger, runway_mode

HOME = Home()


def ac(east_m, north_m, track, gs=150, alt_ft=1500, vrate=-700, cat="A3", hexid="aa1", ground=False):
    lat = HOME.lat + math.degrees(north_m / 6371000.0)
    lon = HOME.lon + math.degrees(east_m / (6371000.0 * math.cos(math.radians(HOME.lat))))
    return Aircraft(hexid, "SAA1", cat, "A320", lat, lon, 0 if ground else alt_ft, ground, gs, track, vrate, 1.0)


def snap(t, aircraft):
    return RadarSnapshot(t, "test", aircraft)


@pytest.fixture
def logger(tmp_path):
    lg = SkyLogger(tmp_path / "sky.sqlite")
    yield lg
    lg.close()


def test_pass_logged_once_at_closest_approach(logger):
    t0 = 1_700_000_000.0
    # inbound from the north, overhead in ~40 s; seen on three consecutive polls
    a1 = ac(200, 3000, 180)
    assert logger.record(snap(t0, [a1]), interval_s=60) == 1
    assert logger.record(snap(t0 + 30, [ac(200, 700, 180)]), interval_s=60) == 0     # same aircraft
    rows = logger.passes()
    assert len(rows) == 1
    ts, level, mode, callsign, closest, alt = rows[0]
    assert ts == pytest.approx(t0 + 3000 / (150 * 0.514444), abs=1)
    assert closest == pytest.approx(200, abs=5) and level > 60


def test_far_high_or_late_aircraft_not_logged(logger):
    t0 = 1_700_000_000.0
    far = ac(6000, 0, 270, hexid="b1")
    high = ac(0, 2000, 180, alt_ft=9000, vrate=0, hexid="b2")
    late = ac(0, 30000, 180, hexid="b3")        # overhead in ~6 min, after the next poll
    assert logger.record(snap(t0, [far, high, late]), interval_s=60) == 0
    assert len(logger.polls()) == 1               # but the poll counts as observed time


def test_runway_mode_inference():
    a, b = FACT_RWY_01.a, FACT_RWY_01.b
    lat, lon = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    dep01 = Aircraft("d1", "", "A3", "", lat + 0.03, lon, 1500, False, 160, 346, 1500, 1)
    arr01 = Aircraft("d2", "", "A3", "", lat - 0.05, lon + 0.01, 2000, False, 140, 346, -700, 1)
    arr19 = Aircraft("d3", "", "A3", "", lat + 0.06, lon, 2500, False, 140, 166, -700, 1)
    assert runway_mode([dep01, arr01]) == "01"
    assert runway_mode([arr19]) == "19"
    assert runway_mode([]) is None


def synth_history(days=10, busy_hours=(7, 8, 18), quiet_hours=(13, 22), start=datetime(2026, 11, 2)):
    """Polls every 60 s; 6 loud passes/h in busy hours, 0 in quiet hours, 2/h otherwise."""
    polls, passes = [], []
    for d in range(days):
        for h in range(24):
            base = (start + timedelta(days=d, hours=h)).timestamp()
            polls += [(base + 60 * k, 60.0, "01") for k in range(60)]
            n = 6 if h in busy_hours else (0 if h in quiet_hours else 2)
            passes += [(base + 3600 * (k + 0.5) / max(n, 1), 75.0, "01") for k in range(n)]
    return passes, polls


def test_forecast_learns_hourly_rates():
    passes, polls = synth_history()
    fc = build(passes, polls, runway_mode="01")
    assert not fc.learning and fc.personal_weight == 1.0
    assert fc.rate[0, 7] == pytest.approx(6.0, abs=0.01)
    assert fc.rate[2, 13] == pytest.approx(0.0, abs=0.01)
    assert fc.rate[4, 10] == pytest.approx(2.0, abs=0.01)


def test_best_windows_pick_the_quiet_hours():
    passes, polls = synth_history()
    fc = build(passes, polls)
    wins = best_windows(fc, now=datetime(2026, 11, 12, 6, 5))
    starts = [w.start.hour for w in wins]
    assert 13 in starts and 22 in starts
    assert all(w.expected < 0.5 for w in wins if w.start.hour in (13, 22))
    assert "Quietest today:" in summary_line(fc, now=datetime(2026, 11, 12, 6, 5))


def test_cold_start_uses_generic_curve_and_says_learning():
    fc = build([], [])
    assert fc.learning and fc.personal_weight == 0.0
    assert np.allclose(fc.rate[3], GENERIC_RATE)
    line = summary_line(fc, now=datetime(2026, 11, 12, 20, 0))
    assert "learning your sky (day 1 of 7)" in line
    # generic quiet hours are the small hours / late evening
    assert best_windows(fc, now=datetime(2026, 11, 12, 20, 0))[-1].start.hour >= 22


def test_partial_history_blends():
    passes, polls = synth_history(days=3)
    fc = build(passes, polls)
    assert 0.4 < fc.personal_weight < 0.45
    assert GENERIC_RATE[13] * 0.5 < fc.rate[0, 13] + 1e-9 < GENERIC_RATE[13]   # pulled toward the observed 0


def test_runway_mode_specific_rates_used():
    p1, q1 = synth_history(days=8, busy_hours=(13,), quiet_hours=(7,))          # mode 01: 13h busy
    p19 = [(ts, lvl, "19") for ts, lvl, _ in synth_history(days=8, busy_hours=(7,), quiet_hours=(13,),
                                                           start=datetime(2026, 11, 10))[0]]
    q19 = [(ts, i, "19") for ts, i, _ in synth_history(days=8, start=datetime(2026, 11, 10))[1]]
    fc01 = build(p1 + p19, q1 + q19, runway_mode="01")
    fc19 = build(p1 + p19, q1 + q19, runway_mode="19")
    assert fc01.rate[0, 13] > fc19.rate[0, 13]
    assert fc19.rate[0, 7] > fc01.rate[0, 7]


def test_two_loggers_do_not_double_count(tmp_path):
    a, b = SkyLogger(tmp_path / "sky.sqlite"), SkyLogger(tmp_path / "sky.sqlite")
    t0 = 1_700_000_000.0
    assert a.record(snap(t0, [ac(200, 3000, 180)]), interval_s=60) == 1
    assert b.record(snap(t0 + 2, [ac(200, 2700, 180)]), interval_s=60) == 0       # same pass
    passes, polls = synth_history(days=1)
    dup = polls + [(ts + 1, i, m) for ts, i, m in polls]                          # 2 loggers polling
    assert build(passes, dup).observed_hours == pytest.approx(build(passes, polls).observed_hours, rel=0.02)
    a.close(); b.close()
