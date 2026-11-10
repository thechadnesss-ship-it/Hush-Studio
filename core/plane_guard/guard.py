"""Guard: combines the sky radar and the ear into the traffic light (CLAUDE.md Module F §1, §4).

    RED    the ear hears it: the take is on hold (capture never stops; a cut is recorded)
    AMBER  radar predicts a loud pass within the lead time, or the ear hears noise rising
    CLEAR  the ear says it's quiet again AND the radar shows nothing inbound → short green flash
    GREEN  speak normally

Only the ear can start or end RED. All times are *take time* (seconds since REC), which is
also what the splicer and the edit list use; the caller converts radar wall-clock timestamps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .ear import Ear, holds_from_events
from .sky_radar import RadarSnapshot, Threat


class Light(str, Enum):
    GREEN = "green"
    AMBER = "amber"
    RED = "red"
    CLEAR = "clear"


@dataclass(frozen=True)
class GuardConfig:
    enabled: bool = True
    clear_flash_s: float = 3.0
    radar_stale_s: float = 20.0      # ignore radar predictions older than this


@dataclass(frozen=True)
class GuardUpdate:
    t: float
    light: Light
    message: str
    eta_s: float | None = None
    chime: bool = False              # soft chime to her headphones (AMBER and CLEAR)


@dataclass
class Guard:
    ear: Ear
    cfg: GuardConfig = field(default_factory=GuardConfig)
    light: Light = Light.GREEN
    history: list[GuardUpdate] = field(default_factory=list)
    _radar: RadarSnapshot | None = None
    _radar_t: float = 0.0
    _clear_until: float | None = None
    _was_red: bool = False

    # ------------------------------------------------------------------ inputs

    def on_radar(self, snap: RadarSnapshot, take_t: float) -> None:
        if snap.feed is not None:            # keep the last good snapshot through feed outages
            self._radar, self._radar_t = snap, take_t

    # ------------------------------------------------------------------ output

    def tick(self, take_t: float) -> GuardUpdate | None:
        """Re-evaluate; returns an update only when the light (or its message) changes."""
        upd = self._evaluate(take_t)
        last = self.history[-1] if self.history else None
        if last is None or (upd.light, upd.message) != (last.light, last.message):
            if last is None or upd.light != last.light:
                upd = GuardUpdate(upd.t, upd.light, upd.message, upd.eta_s,
                                  chime=upd.light in (Light.AMBER, Light.CLEAR))
            self.history.append(upd)
            self.light = upd.light
            return upd
        return None

    def inbound(self, take_t: float) -> list[Threat]:
        if self._radar is None or take_t - self._radar_t > self.cfg.radar_stale_s:
            return []
        dt = take_t - self._radar_t
        return [th for th in self._radar.threats if th.eta_s - dt > -10]

    def _evaluate(self, t: float) -> GuardUpdate:
        if not self.cfg.enabled:
            return GuardUpdate(t, Light.GREEN, "Guard off.")
        if self.ear.state == "calibrating":
            return GuardUpdate(t, Light.GREEN, "Soundcheck… stay quiet for a few seconds.")
        if self.ear.state == "red":
            self._was_red, self._clear_until = True, None
            return GuardUpdate(t, Light.RED, "Holding… plane overhead. I'll tell you when it's clear.")
        threats = self.inbound(t)
        if self._was_red:                    # just came out of RED
            self._was_red = False
            if not threats:
                self._clear_until = t + self.cfg.clear_flash_s
        if self._clear_until is not None:
            if t < self._clear_until and not threats:
                return GuardUpdate(t, Light.CLEAR, "All clear! Carry on whenever you're ready.")
            self._clear_until = None
        if threats:
            th = threats[0]
            eta = max(0.0, th.eta_s - (t - self._radar_t))
            return GuardUpdate(t, Light.AMBER, f"Plane in ~{eta:.0f}s, finish your sentence. ({th.describe()})", eta)
        if self.ear.rising:
            return GuardUpdate(t, Light.AMBER, "Something's getting louder… finish your sentence.")
        return GuardUpdate(t, Light.GREEN, "Listening…")

    # ------------------------------------------------------------------ for the splicer

    def holds(self, end_t: float) -> list[tuple[float, float]]:
        return holds_from_events(self.ear.events, end_t)

    def onsets(self) -> list[float]:
        return [e.t for e in self.ear.events if e.kind == "rising"]
