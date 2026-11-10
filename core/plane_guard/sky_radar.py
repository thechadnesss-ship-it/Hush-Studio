"""Sky radar: early warning from public ADS-B feeds (CLAUDE.md Module F §2).

Polls adsb.lol (no key), falls back to airplanes.live, then OpenSky. For every aircraft it
projects the current track forward, finds the closest approach to home (time, horizontal
distance, altitude there) and scores how loud that pass will be. AMBER when a loud pass is
less than `amber_lead_s` away.

Cape Town (FACT) trick: runway 01 departures climb out over Parow only ~60–90 s after the
takeoff roll starts, so an aircraft accelerating on runway 01 raises AMBER immediately.

The radar never sets or clears RED: ADS-B misses helicopters, some light and military
aircraft. Only the ear can judge what's actually audible.

Privacy: home coordinates are rounded to 0.01° (~1 km) before any request leaves the machine.
"""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

FT = 0.3048
KT = 0.514444
NM = 1852.0
EARTH_R = 6371000.0
from ..app_info import USER_AGENT  # noqa: E402  (one place for the app name)

# Rough source level by ADS-B emitter category, dB at 300 m slant range. Relative only; the
# quiet-time planner's logs (radar pass vs. what the ear heard) are the place to calibrate it.
CATEGORY_LEVEL_DB = {
    "A1": 78,   # light (< 15 500 lb): Cessna, SLG2 trainers
    "A2": 84,   # small: bizjets, turboprops
    "A3": 92,   # large: A320 / B737
    "A4": 94,   # high-vortex large: B757
    "A5": 98,   # heavy: B777 / A350
    "A6": 95,   # high performance
    "A7": 88,   # rotorcraft
    "B1": 60, "B2": 60, "B4": 70, "B6": 70,   # glider, balloon, ultralight, drone
}
DEFAULT_LEVEL_DB = 90
UNKNOWN_ALT_FT = 1000.0


@dataclass(frozen=True)
class Home:
    lat: float = -33.91
    lon: float = 18.61

    def rounded(self) -> "Home":
        return Home(round(self.lat, 2), round(self.lon, 2))


@dataclass(frozen=True)
class Runway:
    """Centreline from threshold `a` (where the takeoff roll starts) to the far end `b`."""
    name: str
    a: tuple[float, float]
    b: tuple[float, float]
    half_width_m: float = 60.0


# FACT 01/19 from OurAirports (true heading 346°). A runway-01 departure rolls from the
# south threshold (01) toward the north end (19) and lifts off heading toward Parow.
FACT_RWY_01 = Runway("FACT 01", a=(-33.9877014, 18.6089001), b=(-33.9598007, 18.6000004))


@dataclass(frozen=True)
class RadarConfig:
    radius_nm: float = 15.0
    poll_recording_s: float = 5.0
    poll_idle_s: float = 60.0
    amber_lead_s: float = 45.0
    loud_threshold_db: float = 62.0        # predicted level at home that counts as "will be audible"
    max_track_age_s: float = 30.0          # ignore positions older than this
    takeoff_min_gs_kt: float = 40.0
    takeoff_heading_tol_deg: float = 25.0
    takeoff_lead_s: float = 75.0           # typical roll start → overhead time for runway 01
    runways: tuple[Runway, ...] = (FACT_RWY_01,)


@dataclass
class Aircraft:
    hex: str
    callsign: str
    category: str
    type: str
    lat: float
    lon: float
    alt_ft: float               # 0 when on the ground
    on_ground: bool
    gs_kt: float
    track_deg: float
    vrate_fpm: float
    age_s: float

    @property
    def label(self) -> str:
        return (self.callsign or self.type or self.hex).strip()


@dataclass(frozen=True)
class Threat:
    aircraft: Aircraft
    eta_s: float                # time until closest approach (or overhead)
    closest_m: float            # horizontal distance at closest approach
    alt_m: float                # altitude at closest approach
    level_db: float             # predicted loudness at home
    reason: str                 # "approach" | "takeoff"

    def describe(self) -> str:
        who = self.aircraft.label
        if self.reason == "takeoff":
            return f"{who} taking off from runway 01, overhead in ~{self.eta_s:.0f}s"
        return f"{who} in ~{self.eta_s:.0f}s ({self.closest_m/1000:.1f} km, {self.alt_m:.0f} m up)"


# =========================================================================== geometry

def to_local(home: Home, lat: float, lon: float) -> tuple[float, float]:
    """Equirectangular projection around home: (east_m, north_m). Accurate to <0.1% within 30 km."""
    x = math.radians(lon - home.lon) * EARTH_R * math.cos(math.radians(home.lat))
    y = math.radians(lat - home.lat) * EARTH_R
    return x, y


def predict(ac: Aircraft, home: Home) -> tuple[float, float, float]:
    """(time_to_closest_s ≥ 0, horizontal_distance_m, altitude_m at that time)."""
    px, py = to_local(home, ac.lat, ac.lon)
    v = ac.gs_kt * KT
    vx, vy = v * math.sin(math.radians(ac.track_deg)), v * math.cos(math.radians(ac.track_deg))
    vv = vx * vx + vy * vy
    t = 0.0 if vv < 1e-6 else max(0.0, -(px * vx + py * vy) / vv)
    dx, dy = px + vx * t, py + vy * t
    alt = max(0.0, ac.alt_ft * FT + ac.vrate_fpm * FT / 60.0 * t)
    return t, math.hypot(dx, dy), alt


def loudness_db(category: str, horizontal_m: float, alt_m: float) -> float:
    slant = max(50.0, math.hypot(horizontal_m, alt_m))
    return CATEGORY_LEVEL_DB.get(category, DEFAULT_LEVEL_DB) - 20 * math.log10(slant / 300.0)


def _angle_diff(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def on_takeoff_roll(ac: Aircraft, rwy: Runway, cfg: RadarConfig) -> bool:
    if not ac.on_ground and ac.alt_ft > 300:
        return False
    if ac.gs_kt < cfg.takeoff_min_gs_kt:
        return False
    ref = Home(*rwy.a)
    bx, by = to_local(ref, *rwy.b)
    px, py = to_local(ref, ac.lat, ac.lon)
    length = math.hypot(bx, by)
    along = (px * bx + py * by) / length
    across = abs(px * by - py * bx) / length
    heading = math.degrees(math.atan2(bx, by)) % 360
    return (-100 <= along <= length + 300 and across <= rwy.half_width_m + 100
            and _angle_diff(ac.track_deg, heading) <= cfg.takeoff_heading_tol_deg)


def assess(aircraft: list[Aircraft], home: Home, cfg: RadarConfig = RadarConfig()) -> list[Threat]:
    """Threats sorted by ETA: loud passes within `amber_lead_s`, plus runway-01 takeoff rolls."""
    out: list[Threat] = []
    for ac in aircraft:
        if ac.age_s > cfg.max_track_age_s:
            continue
        rolling = next((r for r in cfg.runways if on_takeoff_roll(ac, r, cfg)), None)
        if rolling is not None:
            # where will it be when it passes us? project along the runway heading after liftoff
            out.append(Threat(ac, cfg.takeoff_lead_s, *_departure_pass(rolling, home), reason="takeoff"))
            continue
        if ac.on_ground:
            continue
        t, d, alt = predict(ac, home)
        lvl = loudness_db(ac.category, d, alt)
        # AMBER warns *before* a pass: skip aircraft already past us (the ear judges what's audible now)
        approaching = t > 0 or d < 1000
        if approaching and lvl >= cfg.loud_threshold_db and t <= cfg.amber_lead_s:
            out.append(Threat(ac, t, d, alt, lvl, "approach"))
    return sorted(out, key=lambda th: th.eta_s)


def _departure_pass(rwy: Runway, home: Home) -> tuple[float, float, float]:
    """(closest horizontal distance, altitude, loudness) for a typical runway departure."""
    ax, ay = to_local(home, *rwy.a)
    bx, by = to_local(home, *rwy.b)
    dx, dy = bx - ax, by - ay
    L = math.hypot(dx, dy)
    ux, uy = dx / L, dy / L
    s = max(L, -(ax * ux + ay * uy))           # along-track distance from threshold to closest point
    closest = abs(ax * uy - ay * ux)
    climb_m = max(0.0, (s - 0.6 * L)) * 0.08  # ~8% gradient after liftoff ≈ 60% down the runway
    return closest, climb_m, loudness_db("A3", closest, climb_m)


# =========================================================================== feeds

def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_readsb(data: dict) -> list[Aircraft]:
    """adsb.lol / airplanes.live (readsb JSON v2)."""
    out = []
    for a in data.get("ac", []) or []:
        if a.get("lat") is None or a.get("lon") is None:
            continue
        alt = a.get("alt_baro")
        if alt is None:
            alt = a.get("alt_geom")
        ground = alt == "ground"
        out.append(Aircraft(
            hex=a.get("hex", ""), callsign=(a.get("flight") or "").strip(), category=a.get("category", ""),
            type=a.get("t", ""), lat=float(a["lat"]), lon=float(a["lon"]),
            # unknown altitude (common for helicopters) → assume a typical 1000 ft, not 0
            alt_ft=0.0 if ground else _f(alt, UNKNOWN_ALT_FT),
            on_ground=ground, gs_kt=_f(a.get("gs")), track_deg=_f(a.get("track")),
            vrate_fpm=_f(a.get("baro_rate") if a.get("baro_rate") is not None else a.get("geom_rate")),
            age_s=_f(a.get("seen_pos"), 0.0),
        ))
    return out


_OPENSKY_CATEGORY = {2: "A1", 3: "A2", 4: "A3", 5: "A4", 6: "A5", 7: "A6", 8: "A7"}


def parse_opensky(data: dict) -> list[Aircraft]:
    """OpenSky /states/all (state vectors are arrays; SI units)."""
    now = data.get("time") or time.time()
    out = []
    for s in data.get("states", []) or []:
        if s[5] is None or s[6] is None:
            continue
        alt_m = s[7] if s[7] is not None else (s[13] or 0.0)
        cat = _OPENSKY_CATEGORY.get(s[17] if len(s) > 17 else None, "")
        out.append(Aircraft(
            hex=s[0] or "", callsign=(s[1] or "").strip(), category=cat, type="",
            lat=float(s[6]), lon=float(s[5]), alt_ft=0.0 if s[8] else alt_m / FT, on_ground=bool(s[8]),
            gs_kt=_f(s[9]) / KT, track_deg=_f(s[10]), vrate_fpm=_f(s[11]) / FT * 60,
            age_s=max(0.0, now - (s[3] or now)),
        ))
    return out


@dataclass
class Feed:
    name: str
    url: Callable[[Home, float], str]
    parse: Callable[[dict], list[Aircraft]]
    backoff_until: float = 0.0
    failures: int = 0


def default_feeds() -> list[Feed]:
    def opensky(h: Home, r_nm: float) -> str:
        dlat = r_nm * NM / 111_320
        dlon = dlat / math.cos(math.radians(h.lat))
        return (f"https://opensky-network.org/api/states/all?lamin={h.lat - dlat:.2f}&lomin={h.lon - dlon:.2f}"
                f"&lamax={h.lat + dlat:.2f}&lomax={h.lon + dlon:.2f}&extended=1")
    return [
        Feed("adsb.lol", lambda h, r: f"https://api.adsb.lol/v2/lat/{h.lat:.2f}/lon/{h.lon:.2f}/dist/{r:.0f}",
             parse_readsb),
        Feed("airplanes.live", lambda h, r: f"https://api.airplanes.live/v2/point/{h.lat:.2f}/{h.lon:.2f}/{r:.0f}",
             parse_readsb),
        Feed("opensky", opensky, parse_opensky),
    ]


def http_get_json(url: str, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


@dataclass
class RadarSnapshot:
    t: float
    feed: str | None
    aircraft: list[Aircraft] = field(default_factory=list)
    threats: list[Threat] = field(default_factory=list)
    error: str | None = None


class SkyRadar:
    """Call `poll()` on a timer from a worker thread (QThread). Not thread-safe by itself."""

    def __init__(self, home: Home = Home(), cfg: RadarConfig = RadarConfig(),
                 feeds: list[Feed] | None = None, fetch: Callable[[str], dict] = http_get_json,
                 clock: Callable[[], float] = time.time):
        self.home = home.rounded()
        self.cfg = cfg
        self.feeds = feeds if feeds is not None else default_feeds()
        self.fetch = fetch
        self.clock = clock

    def poll(self) -> RadarSnapshot:
        now = self.clock()
        errors = []
        for feed in self.feeds:
            if now < feed.backoff_until:
                continue
            try:
                data = self.fetch(feed.url(self.home, self.cfg.radius_nm))
                aircraft = feed.parse(data)
            except urllib.error.HTTPError as e:
                self._backoff(feed, now, long=e.code == 429)
                errors.append(f"{feed.name}: HTTP {e.code}")
                continue
            except Exception as e:          # network down, timeout, bad JSON → next feed
                self._backoff(feed, now)
                errors.append(f"{feed.name}: {type(e).__name__}")
                continue
            feed.failures = 0
            return RadarSnapshot(now, feed.name, aircraft, assess(aircraft, self.home, self.cfg))
        return RadarSnapshot(now, None, error="; ".join(errors) or "all feeds backing off")

    def _backoff(self, feed: Feed, now: float, long: bool = False) -> None:
        feed.failures += 1
        base = 60.0 if long else 5.0
        feed.backoff_until = now + min(600.0, base * 2 ** (feed.failures - 1))
