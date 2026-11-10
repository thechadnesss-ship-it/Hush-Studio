"""Sky radar prediction, feed fallback, and the guard's traffic light."""
import math
import os
import urllib.error

import numpy as np
import pytest

from core.plane_guard.ear import Ear
from core.plane_guard.guard import Guard, Light
from core.plane_guard.sky_radar import (FACT_RWY_01, Aircraft, Home, RadarConfig, SkyRadar, assess,
                                        default_feeds, on_takeoff_roll, parse_opensky, parse_readsb,
                                        predict, to_local)
from synth import SR, plane, speech_take

HOME = Home()  # De Tyger default, -33.91 18.61


def ac_at(east_m, north_m, track, gs_kt=150, alt_ft=1500, vrate=0, cat="A3", ground=False, age=0.5):
    lat = HOME.lat + math.degrees(north_m / 6371000.0)
    lon = HOME.lon + math.degrees(east_m / (6371000.0 * math.cos(math.radians(HOME.lat))))
    return Aircraft("abc123", "SAA301", cat, "A320", lat, lon, 0.0 if ground else alt_ft, ground,
                    gs_kt, track, vrate, age)


def test_projection_round_trip():
    a = ac_at(3000, -4000, 0)
    x, y = to_local(HOME, a.lat, a.lon)
    assert x == pytest.approx(3000, abs=1) and y == pytest.approx(-4000, abs=1)


def test_closest_approach_geometry():
    # 5 km north, flying due south at 150 kt straight over home
    t, d, alt = predict(ac_at(0, 5000, 180), HOME)
    assert t == pytest.approx(5000 / (150 * 0.514444), rel=1e-3)
    assert d == pytest.approx(0, abs=1)
    # passing 2 km to the east
    t, d, _ = predict(ac_at(2000, 5000, 180), HOME)
    assert d == pytest.approx(2000, abs=1)
    # flying away: closest approach is now
    t, d, _ = predict(ac_at(0, 5000, 0), HOME)
    assert t == 0 and d == pytest.approx(5000, abs=1)


def test_descending_arrival_altitude_projected():
    _, _, alt = predict(ac_at(0, 5000, 180, alt_ft=3000, vrate=-800), HOME)
    t = 5000 / (150 * 0.514444)
    assert alt == pytest.approx(3000 * 0.3048 - 800 * 0.3048 / 60 * t, rel=1e-3)


def test_inbound_jet_raises_threat_with_eta():
    th = assess([ac_at(500, 2500, 180, alt_ft=1500)], HOME)
    assert len(th) == 1 and th[0].reason == "approach"
    assert th[0].eta_s == pytest.approx(2500 / (150 * 0.514444), rel=0.01)


def test_far_departing_or_quiet_traffic_ignored():
    away = ac_at(0, 3000, 0)                               # heading away
    far = ac_at(12000, 0, 270, cat="A1", gs_kt=90)         # light aircraft passing 12 km... far
    late = ac_at(0, 40000, 180)                            # loud, but ~8 min out
    ground = ac_at(0, 1000, 180, gs_kt=10, ground=True)    # taxiing
    stale = ac_at(0, 2000, 180, age=120)
    assert assess([away, far, late, ground, stale], HOME) == []


def test_runway_01_takeoff_roll_warns_early():
    a, b = FACT_RWY_01.a, FACT_RWY_01.b
    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    heading = 346.0
    roll = Aircraft("t1", "SAA302", "A3", "A320", mid[0], mid[1], 0.0, True, 95, heading, 0, 0.3)
    assert on_takeoff_roll(roll, FACT_RWY_01, RadarConfig())
    th = assess([roll], HOME)
    assert len(th) == 1 and th[0].reason == "takeoff" and "runway 01" in th[0].describe()
    # landing roll on 19 (opposite direction), or slow taxi: not a departure toward us
    landing = Aircraft("t2", "", "A3", "", mid[0], mid[1], 0.0, True, 95, 166.0, 0, 0.3)
    taxi = Aircraft("t3", "", "A3", "", mid[0], mid[1], 0.0, True, 15, heading, 0, 0.3)
    assert not on_takeoff_roll(landing, FACT_RWY_01, RadarConfig())
    assert not on_takeoff_roll(taxi, FACT_RWY_01, RadarConfig())


def test_parse_readsb_ground_and_missing_fields():
    data = {"ac": [
        {"hex": "a", "flight": "SAA1 ", "category": "A3", "t": "A320", "lat": -33.9, "lon": 18.6,
         "alt_baro": "ground", "gs": 12, "track": 10, "seen_pos": 1.0},
        {"hex": "b", "lat": -33.8, "lon": 18.5, "alt_geom": 2000, "gs": 140, "track": 180, "geom_rate": -512},
        {"hex": "c"},  # no position: skipped
        {"hex": "d", "lat": -33.8, "lon": 18.5, "gs": 100, "track": 0, "category": "A7"},  # heli, no altitude
    ]}
    acs = parse_readsb(data)
    assert [a.hex for a in acs] == ["a", "b", "d"]
    assert acs[2].alt_ft == 1000 and not acs[2].on_ground
    assert acs[0].on_ground and acs[0].alt_ft == 0 and acs[0].callsign == "SAA1"
    assert acs[1].alt_ft == 2000 and acs[1].vrate_fpm == -512


def test_parse_opensky_units():
    data = {"time": 1000, "states": [
        ["abc", "SAA2    ", "ZA", 995, 999, 18.6, -33.9, 457.2, False, 77.17, 180.0, -2.54, None, 480, None, False, 0, 4],
    ]}
    a, = parse_opensky(data)
    assert a.alt_ft == pytest.approx(1500, rel=1e-3) and a.gs_kt == pytest.approx(150, rel=1e-3)
    assert a.vrate_fpm == pytest.approx(-500, rel=1e-2) and a.category == "A3" and a.age_s == 5


def test_feed_fallback_and_backoff():
    calls = []

    def fetch(url):
        calls.append(url)
        if "adsb.lol" in url:
            raise urllib.error.HTTPError(url, 429, "rate limited", None, None)
        return {"ac": []}

    clock = [1000.0]
    r = SkyRadar(fetch=fetch, clock=lambda: clock[0])
    s = r.poll()
    assert s.feed == "airplanes.live" and s.error is None
    calls.clear()
    clock[0] += 5
    r.poll()
    assert not any("adsb.lol" in u for u in calls)          # still backing off after a 429


def test_home_is_rounded_before_leaving_machine():
    r = SkyRadar(home=Home(-33.912345, 18.614321))
    url = r.feeds[0].url(r.home, 15)
    assert "-33.91" in url and "18.61" in url and "912345" not in url


@pytest.mark.skipif(os.environ.get("PIKA_OFFLINE") == "1", reason="offline")
def test_live_adsb_lol_feed():
    """One real request (set PIKA_OFFLINE=1 to skip)."""
    s = SkyRadar(feeds=default_feeds()[:1]).poll()
    if s.feed is None:
        pytest.skip(f"feed unreachable: {s.error}")
    assert isinstance(s.aircraft, list)


# ----------------------------------------------------------------- guard

class FakeSnap:
    def __init__(self, threats):
        self.feed, self.threats = "test", threats


def drive(guard, x, t0=0.0, step=0.1):
    out = []
    n = int(step * SR)
    for i in range(0, len(x), n):
        guard.ear.feed(x[i:i + n])
        u = guard.tick(t0 + (i + n) / SR)
        if u:
            out.append(u)
    return out


def test_guard_red_then_clear_with_chimes():
    x, _ = speech_take(180.0, -24, seed=5)
    g = Guard(Ear(SR))
    ups = drive(g, x + plane(180.0, 100.0, -30))
    lights = [u.light for u in ups if not ups or True]
    seq = [l for i, l in enumerate(lights) if i == 0 or l != lights[i - 1]]
    assert seq == [Light.GREEN, Light.AMBER, Light.RED, Light.CLEAR, Light.GREEN], seq
    chimes = [u.light for u in ups if u.chime]
    assert chimes == [Light.AMBER, Light.CLEAR]


def test_radar_threat_gives_amber_with_countdown():
    x, _ = speech_take(30.0, -24, seed=7)
    g = Guard(Ear(SR))
    drive(g, x[: 12 * SR])                                  # past the soundcheck
    th = assess([ac_at(0, 3000, 180)], HOME)
    g.on_radar(FakeSnap(th), take_t=12.0)
    u = g.tick(12.0)
    assert u.light == Light.AMBER and u.eta_s == pytest.approx(th[0].eta_s, abs=0.1)
    u = g.tick(17.0)
    assert u.eta_s == pytest.approx(th[0].eta_s - 5, abs=0.1)
    assert g.tick(12.0 + 25.0).light == Light.GREEN        # radar snapshot went stale


def test_no_all_clear_while_another_plane_is_inbound():
    x, _ = speech_take(180.0, -24, seed=5)
    y = x + plane(180.0, 100.0, -30)
    g = Guard(Ear(SR))
    n = int(0.1 * SR)
    lights = []
    for i in range(0, len(y), n):
        t = (i + n) / SR
        g.ear.feed(y[i:i + n])
        if 120 <= t and g.ear.state == "red":
            g.on_radar(FakeSnap(assess([ac_at(0, 3000, 180)], HOME)), take_t=t)
        u = g.tick(t)
        if u:
            lights.append(u.light)
    after_red = lights[lights.index(Light.RED) + 1:]
    assert after_red and after_red[0] == Light.AMBER and Light.CLEAR not in after_red[:2]


def test_guard_off():
    g = Guard(Ear(SR), cfg=type(Guard(Ear(SR)).cfg)(enabled=False))
    g.ear.feed(np.zeros(SR * 12, dtype=np.float32))
    assert g.tick(12.0).light == Light.GREEN
