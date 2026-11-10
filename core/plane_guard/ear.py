"""The ear: hears planes (and helicopters, traffic, anything without a transponder) in the mic.

Background noise is tracked in the 50–2000 Hz band where jet roar lives, with two tricks so
Sam's own voice is never mistaken for noise, even her slow, sustained hypnotic vowels:

1. **Between the harmonics:** voiced speech is a comb of harmonics with quiet valleys between
   them; plane roar is broadband and fills the valleys. Each 64 ms analysis window (15.6 Hz
   resolution) is summarised by a low percentile of its band spectrum, bias-corrected so that
   for white noise it equals the band power. (It under-reads rumble-heavy noise by several dB,
   so RED comes a few seconds late; the splicer compensates by cutting from the "rising" onset.)
2. **Minimum statistics:** the quietest of those estimates over the last 1 s, so syllable gaps,
   breaths and pauses expose the background too.

A plane is continuous and broadband, so it lifts both; speech lifts neither. This works on the
raw mic with no denoiser; if a denoised signal is supplied, `raw − clean` is a second opinion.
(Measured on Sam's real 33 min Riverside take: a plain band-energy minimum raised 8 false REDs
on long voiced phrases.)

Light semantics (CLAUDE.md Module F, plus a room-relative trigger):
  RISING when background is `amber_rise_db` above the room floor for `amber_hold_s` (early warning)
  RED   when background is within `red_snr_db` of her speaking level, OR `red_rise_db` above the
        room floor (whichever is lower), for > `red_hold_s`
  CLEAR when the median background over `clear_hold_s` is within `clear_margin_db` of the room
Hysteresis is built in (the two thresholds are far apart), so the state never flickers.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class EarConfig:
    band_hz: tuple[float, float] = (50.0, 2000.0)
    sub_s: float = 0.020            # analysis hop
    window_s: float = 0.064         # analysis window (fine enough to see between harmonics)
    valley_pct: float = 20.0        # percentile of band bins used as the noise estimate
    floor_window_s: float = 1.0     # minimum-statistics window
    floor_bias_db: float = 2.0      # min of a noisy estimate sits ~2 dB under the true mean
    calibration_s: float = 10.0     # soundcheck at REC
    red_snr_db: float = 12.0
    # A plane far quieter than her voice is still audible in a hushed hypnosis take (and would
    # "pump" once pauses become digital silence), so RED also fires when the background rises
    # this far above the room floor. Whichever threshold is lower wins.
    # Thresholds measured on Sam's real 33 min take (no planes): speech alone lifts the estimate
    # above +15 dB for at most 0.98 s and above +25 dB for at most 0.54 s.
    red_rise_db: float = 25.0
    amber_rise_db: float = 15.0     # "rising" early warning (no-transponder aircraft, traffic)
    amber_hold_s: float = 2.0
    red_hold_s: float = 1.0
    # CLEAR uses the *median* background over `clear_hold_s`: speech blips can't reset it.
    clear_margin_db: float = 6.0
    clear_hold_s: float = 6.0
    default_speech_gap_db: float = 35.0  # speech level if she didn't talk in the soundcheck
    # Sub-frames below this are a noise gate's digital silence (e.g. Riverside exports), not a
    # real room; a live mic never gets this quiet. They're ignored by the floor estimate.
    gate_db: float = -90.0


@dataclass(frozen=True)
class EarEvent:
    t: float            # seconds since REC (raw-take time)
    kind: str           # "calibrated" | "rising" | "red" | "clear"
    floor_db: float
    speech_db: float
    room_db: float


@dataclass
class EarReading:
    t: float
    band_db: float      # current band level (speech + noise)
    floor_db: float     # background estimate
    state: str          # "calibrating" | "green" | "red"


class Ear:
    def __init__(self, sample_rate: int = 48000, cfg: EarConfig = EarConfig()):
        self.sr = sample_rate
        self.cfg = cfg
        self.n = int(round(sample_rate * cfg.sub_s))          # hop
        self.w = max(self.n, int(round(sample_rate * cfg.window_s)))  # window
        self.win = np.hanning(self.w).astype(np.float32)
        f = np.fft.rfftfreq(self.w, 1 / sample_rate)
        self.band = (f >= cfg.band_hz[0]) & (f < cfg.band_hz[1])
        self.nbins = int(self.band.sum())
        self.norm = float((self.win ** 2).sum())
        # For noise, each bin's power is ~exponential: its p-th percentile is −ln(1−p)·mean.
        self.valley_corr = 1.0 / -np.log(1 - cfg.valley_pct / 100.0)
        self._buf = np.zeros(self.w - self.n, dtype=np.float32)   # pre-roll so the first hop has a full window
        self._cbuf = np.zeros(self.w - self.n, dtype=np.float32)
        self._hist = deque(maxlen=max(1, int(round(cfg.floor_window_s / cfg.sub_s))))
        self.t = 0.0
        self.state = "calibrating"
        self.room_db: float | None = None
        self.speech_db: float | None = None
        self._cal_floor: list[float] = []
        self._cal_band: list[float] = []
        self._loud_since: float | None = None
        self._recent: deque[float] = deque(maxlen=max(1, int(round(cfg.clear_hold_s / cfg.sub_s))))
        self._rising_since: float | None = None
        self._rising = False
        self.readings: list[EarReading] = []
        self.events: list[EarEvent] = []

    # ------------------------------------------------------------------ public

    def feed(self, raw: np.ndarray, clean: np.ndarray | None = None) -> list[EarEvent]:
        """Feed any-length mono float chunks (raw mic, optionally the denoised version)."""
        raw = np.asarray(raw, dtype=np.float32).reshape(-1)
        self._buf = np.concatenate([self._buf, raw])
        use_clean = clean is not None
        if use_clean:
            self._cbuf = np.concatenate([self._cbuf, np.asarray(clean, dtype=np.float32).reshape(-1)])
        new: list[EarEvent] = []
        while len(self._buf) >= self.w and (not use_clean or len(self._cbuf) >= self.w):
            fr = self._buf[: self.w]
            self._buf = self._buf[self.n:]
            band_db, valley_db = self._analyse(fr)
            noise_db = None
            if use_clean:
                cf = self._cbuf[: self.w]
                self._cbuf = self._cbuf[self.n:]
                noise_db = self._analyse(fr - cf)[0]
            self._hist.append(valley_db)
            open_ = [v for v in self._hist if v > self.cfg.gate_db]
            # gated window = nothing but digital silence: treat as perfectly quiet
            floor_db = (min(open_) + self.cfg.floor_bias_db) if open_ else self.cfg.gate_db
            if noise_db is not None:
                floor_db = max(floor_db, noise_db)   # second opinion: whichever hears more noise
            self.t += self.cfg.sub_s
            ev = self._step(band_db, floor_db)
            if ev:
                new.append(ev)
        self.events.extend(new)
        return new

    @property
    def red_threshold_db(self) -> float | None:
        if self.speech_db is None or self.room_db is None:
            return None
        return min(self.speech_db - self.cfg.red_snr_db, self.room_db + self.cfg.red_rise_db)

    @property
    def rising(self) -> bool:
        """Background is clearly up but not (yet) RED."""
        return self.state == "green" and self._rising

    @property
    def amber_threshold_db(self) -> float | None:
        return None if self.room_db is None else self.room_db + self.cfg.amber_rise_db

    @property
    def clear_threshold_db(self) -> float | None:
        return None if self.room_db is None else self.room_db + self.cfg.clear_margin_db

    # ------------------------------------------------------------------ internals

    def _analyse(self, fr: np.ndarray) -> tuple[float, float]:
        """(band power dB, between-harmonics noise estimate dB) of one analysis window."""
        bins = np.abs(np.fft.rfft(fr * self.win)[self.band]) ** 2
        scale = 2.0 / (self.norm * self.w)                     # Parseval, one-sided
        band = bins.sum() * scale
        # One percentile over the whole band. (Per-octave was tried: her voice's fundamental
        # fills the small low octaves and false alarms came back on the real take.)
        valley = np.percentile(bins, self.cfg.valley_pct) * self.valley_corr * self.nbins * scale
        return float(10 * np.log10(band + 1e-14)), float(10 * np.log10(valley + 1e-14))

    def _step(self, band_db: float, floor_db: float) -> EarEvent | None:
        c = self.cfg
        if self.state == "calibrating":
            self._cal_floor.append(floor_db)
            self._cal_band.append(band_db)
            self.readings.append(EarReading(self.t, band_db, floor_db, self.state))
            if self.t + 1e-9 >= c.calibration_s:
                open_floors = [v for v in self._cal_floor if v > c.gate_db]
                # room = background while the mic/gate is open; all-gated soundcheck → gate level
                self.room_db = float(np.median(open_floors)) if open_floors else c.gate_db
                loud = float(np.percentile(self._cal_band, 90))
                # did she speak during the soundcheck? if not, assume a typical speech level
                self.speech_db = loud if loud - self.room_db > 15 else self.room_db + c.default_speech_gap_db
                self.state = "green"
                return self._event("calibrated", floor_db)
            return None

        # slowly learn her real speaking level while green (soundcheck may have been short)
        if self.state == "green" and band_db > floor_db + 15:
            self.speech_db = max(self.speech_db, 0.999 * self.speech_db + 0.001 * band_db)

        self.readings.append(EarReading(self.t, band_db, floor_db, self.state))
        if self.state == "green":
            # early warning: background clearly rising, not yet RED
            if floor_db > self.amber_threshold_db:
                self._rising_since = self._rising_since if self._rising_since is not None else self.t
                if not self._rising and self.t - self._rising_since >= c.amber_hold_s:
                    self._rising = True
                    return self._event("rising", floor_db, at=self.t - c.amber_hold_s)
            elif floor_db < self.room_db + c.amber_rise_db / 2:
                self._rising_since, self._rising = None, False
            if floor_db > self.red_threshold_db:
                self._loud_since = self._loud_since if self._loud_since is not None else self.t
                if self.t - self._loud_since >= c.red_hold_s:
                    self.state, self._loud_since = "red", None
                    self._recent.clear()
                    return self._event("red", floor_db, at=self.t - c.red_hold_s)
            else:
                self._loud_since = None
        elif self.state == "red":
            self._recent.append(floor_db)
            if (len(self._recent) == self._recent.maxlen
                    and float(np.median(self._recent)) <= self.clear_threshold_db):
                self.state = "green"
                self._recent.clear()
                self._rising_since, self._rising = None, False
                return self._event("clear", floor_db)
        return None

    def _event(self, kind: str, floor_db: float, at: float | None = None) -> EarEvent:
        return EarEvent(round(self.t if at is None else at, 3), kind, floor_db,
                        self.speech_db or 0.0, self.room_db or 0.0)


def holds_from_events(events: list[EarEvent], end_t: float) -> list[tuple[float, float]]:
    """[(red_t, clear_t)] hold regions; an unfinished hold runs to `end_t`."""
    out, red = [], None
    for e in events:
        if e.kind == "red" and red is None:
            red = e.t
        elif e.kind == "clear" and red is not None:
            out.append((red, e.t))
            red = None
    if red is not None:
        out.append((red, end_t))
    return out
