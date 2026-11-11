"""Mic calibration (T27): measure the room and her voice once per microphone, then record at a
healthy level.

Sam's first real take peaked at −29.5 dBFS on the Samson Q2U, 20+ dB too quiet, and nothing told
her. The calibration step fixes that before she records:

1. setup checks: which mic (USB vs laptop array vs Bluetooth headset), a noise-suppression device
   in the chain (Krisp, NVIDIA Broadcast…), 48 kHz support, speakers vs headphones;
2. 5 s of quiet → the room floor;
3. she reads a sentence at her normal volume → speech level and peak;
4. plain advice (turn the mic up / down in Windows, move closer) and an input gain that puts her
   sentence's peak at `TARGET_PEAK_DB`, so a whole take peaks in `ZONE` (−12…−6 dBFS).

The result is stored per device (QSettings → HKCU on Windows), the Recorder applies the gain to
every block before raw.flac, and `LevelWatch` warns live if her level drifts out of range.

Pure logic: no Qt, no audio hardware; the store takes any QSettings-like object.
"""
from __future__ import annotations

import json
import re
import time
from collections import deque
from dataclasses import asdict, dataclass, field

import numpy as np

from .diagnostics import flat_top_runs

SR = 48000
BLOCK = 4800                     # 100 ms, same as capture.BLOCK
CLIP_LEVEL = 0.999               # same as capture.CLIP_LEVEL

QUIET_S = 5.0                    # room floor measurement
READ_S = 12.0                    # longest sentence recording
MIN_SPEECH_S = 1.5               # less than this = "we couldn't hear you"

ZONE = (-12.0, -6.0)             # where a normal-voice take should peak (dBFS)
TARGET_PEAK_DB = -10.0           # the calibration sentence's peak after gain: a 30 min take peaks a
                                 # few dB above one sentence, which lands it inside ZONE
MAX_GAIN_DB = 30.0               # beyond this the mic has to be turned up in Windows
MIN_GAIN_DB = -18.0
STRONG_BOOST_DB = 12.0           # more boost than this → also suggest turning the mic up
GATED_FLOOR_DB = -90.0           # a "room" this quiet is digital silence: something gates the mic
SPEECH_OVER_FLOOR_DB = 10.0      # a block counts as voice this far above the room floor
MIN_SNR_DB = 15.0                # voice vs room; below this the background is clearly audible
GOOD_SNR_DB = 25.0

READ_SENTENCE = ("Let your breathing slow down now… and with every breath, your body feels heavier, "
                 "softer and more relaxed, as you drift gently towards a deep and peaceful sleep.")

_SOUND_SETTINGS = ("Windows Settings → System → Sound → under Input choose your microphone → "
                   "Properties → Volume")


def db(x: float) -> float:
    return float(20.0 * np.log10(max(float(x), 1e-12)))


def block_levels(x: np.ndarray, block: int = BLOCK) -> tuple[np.ndarray, np.ndarray]:
    """(RMS dBFS, peak dBFS) of each 100 ms block (a short tail block counts too)."""
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    if not x.size:
        return np.zeros(0), np.zeros(0)
    n = -(-x.size // block)
    pad = np.zeros(n * block, np.float32)
    pad[:x.size] = x
    b = pad.reshape(n, block).astype(np.float64)
    lens = np.full(n, block, dtype=np.float64)
    lens[-1] = x.size - (n - 1) * block
    rms = np.sqrt((b * b).sum(axis=1) / lens)
    peak = np.abs(b).max(axis=1)
    return 20 * np.log10(np.maximum(rms, 1e-12)), 20 * np.log10(np.maximum(peak, 1e-12))


# =========================================================================== measuring

@dataclass(frozen=True)
class Floor:
    rms_db: float                # median 100 ms RMS of the quiet part
    peak_db: float
    digital_silence: bool        # exact zeros / below −90 dBFS: a noise gate or suppressor is on


def measure_floor(x: np.ndarray, skip_s: float = 0.3) -> Floor:
    """The room floor from the quiet recording. The first `skip_s` is skipped (driver start-up,
    the click of the button)."""
    x = np.asarray(x, dtype=np.float32).reshape(-1)[int(skip_s * SR):]
    rms, peak = block_levels(x)
    if not rms.size:
        return Floor(-120.0, -120.0, True)
    floor = float(np.median(rms))
    return Floor(round(floor, 1), round(float(peak.max()), 1), floor <= GATED_FLOOR_DB)


@dataclass(frozen=True)
class Speech:
    peak_db: float               # sample peak of the sentence
    rms_db: float                # RMS of the voiced blocks only
    active_s: float              # how much of it was voice
    clipped: int                 # samples at full scale + flat-top runs: the mic itself is too hot


def measure_speech(x: np.ndarray, floor_db: float) -> Speech:
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    rms, _ = block_levels(x)
    if not rms.size:
        return Speech(-120.0, -120.0, 0.0, 0)
    voiced = rms > max(floor_db + SPEECH_OVER_FLOOR_DB, -70.0)
    a = np.abs(x)
    clipped = int(np.count_nonzero(a >= CLIP_LEVEL)) + int(flat_top_runs(x, min_len=8, relative_to_peak=False))
    if not voiced.any():
        return Speech(round(db(a.max(initial=0.0)), 1), -120.0, 0.0, clipped)
    lin = 10 ** (rms[voiced] / 10)                # power average of the voiced blocks
    return Speech(round(db(a.max(initial=0.0)), 1), round(float(10 * np.log10(lin.mean())), 1),
                  round(float(voiced.sum() * BLOCK / SR), 1), clipped)


def gain_for(peak_db: float) -> float:
    """Input gain (dB) that moves the sentence peak to TARGET_PEAK_DB, clamped, in 0.5 dB steps.
    Within 1 dB of target nothing is changed."""
    g = TARGET_PEAK_DB - peak_db
    if abs(g) < 1.0:
        return 0.0
    return float(np.clip(round(g * 2) / 2, MIN_GAIN_DB, MAX_GAIN_DB))


# =========================================================================== advice

@dataclass(frozen=True)
class Advice:
    code: str
    level: str                   # "ok" | "info" | "warn" | "bad" (bad = fix it and try again)
    title: str
    text: str = ""


def _fmt_db(g: float) -> str:
    return f"{g:+.0f} dB" if abs(g - round(g)) < 0.25 else f"{g:+.1f} dB"


def level_advice(floor: Floor, speech: Speech) -> list[Advice]:
    """Plain words about what she heard and what to change. Worst first."""
    out: list[Advice] = []
    if speech.active_s < MIN_SPEECH_S:
        if speech.peak_db <= -119 and floor.rms_db <= -119:
            return [Advice("silent", "bad", "The microphone isn't sending any sound",
                           "Check the mic isn't muted (a mute button on the mic, or "
                           f"{_SOUND_SETTINGS}), and that you chose the right microphone above. "
                           "Then try again.")]
        return [Advice("no_speech", "bad", "We couldn't hear you reading",
                       "Check you chose the microphone you're speaking into, that it isn't muted, and "
                       "read the whole sentence at your normal voice. Then try again.")]
    if speech.clipped or speech.peak_db >= -0.5:
        out.append(Advice("clipping", "bad", "Your mic is too loud: your voice is clipping",
                          "Clipping sounds harsh and can't be fixed afterwards. Turn the mic level down "
                          f"({_SOUND_SETTINGS}) to about half, or sit a little further back, then try again."))
    g = TARGET_PEAK_DB - speech.peak_db
    if not out:
        if g > MAX_GAIN_DB:
            out.append(Advice("too_quiet", "bad", "Your voice is far too quiet",
                              f"Even the biggest boost Hush Studio can add isn't enough. Turn the mic up to "
                              f"100 ({_SOUND_SETTINGS}), speak about a hand's width from the mic, and check "
                              "this is the microphone you're speaking into. Then try again."))
        elif g > STRONG_BOOST_DB:
            out.append(Advice("quiet", "warn", "Your voice is coming in quiet",
                              f"Hush Studio will boost it by {_fmt_db(gain_for(speech.peak_db))} so the take "
                              f"comes out right. It sounds cleaner if you turn the mic up instead ({_SOUND_SETTINGS}, "
                              "about 80–100) or move a little closer, then try again."))
        elif speech.peak_db > ZONE[1]:
            out.append(Advice("loud", "info", "Your voice is a little loud",
                              f"That's fine: Hush Studio will turn it down by {-gain_for(speech.peak_db):.0f} dB. "
                              "If it clips later, turn the mic level down a little in Windows."))
    if floor.digital_silence:
        out.append(Advice("gated", "warn", "Something is removing noise before Hush Studio hears it",
                          "The quiet part was completely silent, so a noise suppressor or gate is switched on "
                          "(Krisp, NVIDIA Broadcast, or Windows “audio enhancements” in your mic's Properties). "
                          "Turn it off: it chops the quiet ends of words. Hush Studio removes noise itself, "
                          "more gently."))
    else:
        snr = speech.rms_db - floor.rms_db
        if snr < MIN_SNR_DB:
            out.append(Advice("noisy", "warn", "The room is loud compared with your voice",
                              f"Your voice is only {snr:.0f} dB above the background ({GOOD_SNR_DB:.0f}+ is good). "
                              "Move closer to the mic, and close windows or switch off fans if you can."))
        elif snr < GOOD_SNR_DB:
            out.append(Advice("snr_ok", "info", "Background noise is fine",
                              f"Your voice is {snr:.0f} dB above the room. A little closer to the mic would make "
                              "the background quieter still."))
    if not any(a.code in ("clipping", "too_quiet", "quiet", "loud") for a in out):
        g = gain_for(speech.peak_db)
        text = ("Your take will be recorded at a healthy level."
                + (f" Hush Studio adds {_fmt_db(g)} to get there." if g else ""))
        out.insert(0, Advice("good", "ok", "Your level is good", text))
    order = {"bad": 0, "warn": 1, "ok": 2, "info": 3}           # problems first, then the verdict
    return sorted(out, key=lambda a: order[a.level])


# =========================================================================== setup checks

_SUPPRESSORS = ("krisp", "nvidia broadcast", "rtx voice", "steelseries sonar", "sonar -", "voicemod",
                "nahimic", "noise cancel", "noise suppress", "ai noise", "noise reduction",
                "voice focus")
_VIRTUAL = ("cable output", "vb-audio", "voicemeeter", "virtual", "steam streaming", "stereo mix", "what u hear", "loopback")
_LAPTOP = ("array", "smart sound", "intel®", "intel(r)", "internal", "built-in", "builtin", "integrated",
           "digital mic", "dmic")
_BLUETOOTH = ("hands-free", "handsfree", "bluetooth", "airpods", "buds", " bt ")
_USB = ("usb", "samson", "q2u", "yeti", "blue ", "rode", "røde", "shure", "audio-technica", "at2020",
        "at2005", "fifine", "hyperx", "quadcast", "elgato", "wave:", "focusrite", "scarlett", "audient",
        "motu", "behringer", "tascam", "pnp", "maono", "tonor", "razer seiren", "sennheiser")
_HEADPHONES = ("headphone", "headset", "earphone", "airpods", "buds", "earbuds", "ear phones")
_SPEAKERS = ("speaker", "display audio", "hdmi", "nvidia high definition", "amd high definition",
             "monitor")


def classify_input(name: str) -> str:
    """'suppressor' | 'virtual' | 'bluetooth' | 'laptop' | 'usb' | 'unknown' from a Windows device name."""
    n = f" {name.lower()} "
    for kind, words in (("suppressor", _SUPPRESSORS), ("virtual", _VIRTUAL), ("bluetooth", _BLUETOOTH),
                        ("usb", _USB), ("laptop", _LAPTOP)):
        if any(w in n for w in words):
            return kind
    return "unknown"


def _brand_words(name: str) -> set[str]:
    """The device part of 'Microphone (Samson Q2U Microphone)' → {'samson', 'q2u'}."""
    m = re.search(r"\(([^)]*)\)", name)
    inner = (m.group(1) if m else name).lower()
    return {w for w in re.findall(r"[a-z0-9]{3,}", inner)} - {"microphone", "audio", "device", "sound",
                                                              "high", "definition", "realtek"}


def classify_output(name: str, mic_name: str = "") -> str:
    """'headphones' | 'speakers' | 'unknown'. Headphones plugged into a USB mic show up as
    'Speakers (<the mic>)', so an output on the same USB device as the mic counts as headphones."""
    n = name.lower()
    if any(w in n for w in _HEADPHONES):
        return "headphones"
    if mic_name and classify_input(mic_name) == "usb" and _brand_words(name) & _brand_words(mic_name):
        return "headphones"
    if any(w in n for w in _SPEAKERS):
        return "speakers"
    return "unknown"


@dataclass
class DeviceSnapshot:
    """What Windows told us about the audio setup (filled by ui/calibrate.py from sounddevice)."""
    chosen: str                              # the mic she'll record with (resolved, never "Default")
    using_default: bool = False
    default_input: str = ""
    inputs: list[str] = field(default_factory=list)
    output: str = ""                         # default playback device
    default_rate: float = 48000.0
    rate_ok: bool = True                     # opens at 48 kHz
    found: bool = True                       # Windows has an input device for this choice


def setup_advice(d: DeviceSnapshot) -> list[Advice]:
    """Checks before she records: the right mic, nothing suppressing noise, 48 kHz, headphones."""
    if not d.found:
        from .mic_check import MESSAGES
        return [Advice("no_device", "bad", *MESSAGES["no_device"])]
    out: list[Advice] = []
    kind = classify_input(d.chosen)
    usb_mics = [n for n in d.inputs if classify_input(n) == "usb"]
    if kind == "suppressor":
        out.append(Advice("suppressor", "bad", f"“{d.chosen}” is a noise-suppression app, not a microphone",
                          "Apps like Krisp or NVIDIA Broadcast chop the quiet ends of words and add a robotic "
                          "sound. Choose your real microphone instead: Hush Studio removes noise itself."))
    elif kind == "virtual":
        out.append(Advice("virtual", "bad", f"“{d.chosen}” isn't a real microphone",
                          "It's a virtual or mixer device. Choose the microphone you speak into."))
    elif kind == "bluetooth":
        out.append(Advice("bluetooth", "warn", "This looks like a Bluetooth headset mic",
                          "Bluetooth headset mics sound like a phone call. Use your USB mic for recordings."))
    elif kind == "laptop":
        if usb_mics:
            what = "Windows' default microphone" if d.using_default else "This"
            out.append(Advice("wrong_mic", "bad", f"{what} is the laptop's built-in mic, but your USB mic is plugged in",
                              f"Choose “{usb_mics[0]}” as the microphone. The built-in mic picks up the room and "
                              "the laptop's fan."))
        else:
            out.append(Advice("laptop", "warn", "This is the laptop's built-in mic",
                              "It hears the whole room and the laptop's fan. A USB mic close to you sounds much "
                              "better. If you have one, plug it in and choose it."))
    if kind not in ("suppressor", "virtual"):
        others = [n for n in d.inputs if classify_input(n) == "suppressor"]
        if others:
            out.append(Advice("suppressor_installed", "info", "A noise-suppression app is installed",
                              f"“{others[0]}” is installed. That's fine as long as you record with your real "
                              "microphone (as now), not through it."))
    if not d.rate_ok:
        out.append(Advice("rate", "bad", "The microphone can't record at 48 kHz",
                          "Set it in Windows: Control Panel → Sound → Recording → your mic → Properties → "
                          "Advanced → choose “1 channel, 24 bit, 48000 Hz” (or 16 bit), press OK, then try again."))
    elif d.default_rate and d.default_rate < 32000:
        out.append(Advice("low_rate", "warn", f"The microphone runs at {d.default_rate / 1000:.0f} kHz",
                          "That's phone quality (usually a Bluetooth headset). Use a USB mic set to 48 kHz."))
    if d.output:
        o = classify_output(d.output, d.chosen)
        if o == "speakers":
            out.append(Advice("speakers", "warn", "Sound is playing through speakers",
                              f"“{d.output}” sounds into the room, so the mic can record beeps or playback. "
                              "Wear headphones while you record, or keep the volume off."))
        elif o == "unknown":
            out.append(Advice("output_unknown", "info", "Use headphones if you can",
                              "Anything played through speakers while you record can end up in the take."))
    if not out or all(a.level in ("ok", "info") for a in out):
        out.insert(0, Advice("setup_ok", "ok", "Your setup looks right", f"Recording with “{d.chosen}”."))
    order = {"bad": 0, "warn": 1, "ok": 2, "info": 3}           # problems first, then the verdict
    return sorted(out, key=lambda a: order[a.level])


# =========================================================================== storing

@dataclass
class Calibration:
    device: str
    floor_db: float
    speech_rms_db: float
    speech_peak_db: float
    gain_db: float
    digital_silence: bool = False
    measured_at: float = 0.0                 # unix time
    advice: list[str] = field(default_factory=list)   # advice codes at the time

    @property
    def expected_peak_db(self) -> float:
        return self.speech_peak_db + self.gain_db

    def summary(self) -> str:
        when = time.strftime("%d %b %Y", time.localtime(self.measured_at)) if self.measured_at else "?"
        boost = f", boost {_fmt_db(self.gain_db)}" if self.gain_db else ""
        return (f"Checked {when}: room {self.floor_db:.0f} dBFS, voice peaks {self.speech_peak_db:.0f} dBFS"
                f"{boost} → {self.expected_peak_db:.0f} dBFS")


def calibrate(device: str, quiet: np.ndarray, reading: np.ndarray) -> tuple[Calibration, list[Advice]]:
    floor = measure_floor(quiet)
    speech = measure_speech(reading, floor.rms_db)
    adv = level_advice(floor, speech)
    usable = speech.active_s >= MIN_SPEECH_S and not speech.clipped
    g = gain_for(speech.peak_db) if usable else 0.0
    cal = Calibration(device, floor.rms_db, speech.rms_db, speech.peak_db, g, floor.digital_silence,
                      time.time(), [a.code for a in adv])
    return cal, adv


def device_key(name: str) -> str:
    """Stable key for a device name. MME truncates names to 31 characters while WASAPI gives the
    full name, so both are cut to 31; QSettings separators are replaced."""
    n = re.sub(r"\s+", " ", (name or "").strip().lower())[:31].strip()
    return re.sub(r"[\\/]", "_", n) or "unknown"


class CalibrationStore:
    """Per-device calibration in QSettings (HKCU\\Software\\<org>\\<app>\\calibration on Windows).
    `settings` needs value(key, default) / setValue / remove (QSettings, or a dict wrapper in tests)."""

    GROUP = "calibration"

    def __init__(self, settings):
        self.s = settings

    def _key(self, device: str) -> str:
        return f"{self.GROUP}/{device_key(device)}"

    def get(self, device: str) -> Calibration | None:
        raw = self.s.value(self._key(device), None)
        if not raw:
            return None
        try:
            d = json.loads(str(raw))
            return Calibration(**{k: d[k] for k in Calibration.__dataclass_fields__ if k in d})
        except (ValueError, TypeError, KeyError):
            return None

    def put(self, cal: Calibration) -> None:
        self.s.setValue(self._key(cal.device), json.dumps(asdict(cal)))

    def forget(self, device: str) -> None:
        self.s.remove(self._key(device))

    def gain_db(self, device: str) -> float:
        cal = self.get(device)
        return float(cal.gain_db) if cal else 0.0


# =========================================================================== live watch

LIVE_QUIET_DB = ZONE[0] - 8.0        # voice peaks below −20 dBFS over the last stretch → too quiet
LIVE_QUIET_CLEAR_DB = ZONE[0] - 5.0
LIVE_LOUD_DB = -2.0                  # peaks this hot (short of clipping) → too loud
LIVE_LOUD_CLEAR_DB = -4.0

LIVE_MESSAGES = {
    "quiet": ("Your voice is coming in quiet. Move a little closer to the mic, or check it's the right "
              "microphone (Settings → Check mic)."),
    "loud": "Your voice is very loud. Sit back a little from the mic so it doesn't clip.",
}


class LevelWatch:
    """Watches the recorded level while she speaks and says when it's out of range.

    Feed it every recorded block's (RMS dB, peak dB). Voiced blocks (above the room floor) are kept
    for the last `window_s` of voice; once `min_voice_s` of voice is in, the loudest peak in that
    window decides: quiet (< −20 dBFS), loud (> −2 dBFS), or fine. Hysteresis stops flicker.
    The floor is the calibrated one, else the 10th percentile of the last 30 s of blocks."""

    def __init__(self, floor_db: float | None = None, window_s: float = 20.0, min_voice_s: float = 5.0):
        self.floor_db = floor_db
        self._recent = deque(maxlen=300)                 # 30 s of block RMS for the floor estimate
        self._voice = deque(maxlen=int(window_s * 10))
        self.min_blocks = int(min_voice_s * 10)
        self.state = ""                                  # "" | "quiet" | "loud"

    def _floor(self) -> float:
        if self.floor_db is not None:
            return self.floor_db
        return float(np.percentile(self._recent, 10)) if self._recent else -120.0

    def feed(self, rms_db: float, peak_db: float) -> str:
        self._recent.append(rms_db)
        if rms_db > max(self._floor() + SPEECH_OVER_FLOOR_DB, -70.0):
            self._voice.append(peak_db)
        if len(self._voice) < self.min_blocks:
            return self.state
        top = max(self._voice)
        if self.state == "quiet":
            if top > LIVE_QUIET_CLEAR_DB:
                self.state = ""
        elif self.state == "loud":
            if top < LIVE_LOUD_CLEAR_DB:
                self.state = ""
        if not self.state:
            if top < LIVE_QUIET_DB:
                self.state = "quiet"
            elif top > LIVE_LOUD_DB:
                self.state = "loud"
        return self.state

    @property
    def message(self) -> str:
        return LIVE_MESSAGES.get(self.state, "")
