"""First-run microphone check (T21): can Hush Studio hear the mic, and if not, why?

Windows has three privacy switches that silently block a desktop app's microphone; when one is
off the app still opens the mic but receives exact digital zeros. So the check reads those
switches (registry, no admin needed) and also listens for half a second. Every problem comes
with plain-language steps; `SETTINGS_URI` opens the right Settings page directly.

Pure logic with injectable registry/listen functions, so it is testable without hardware.

T30 adds the per-mic *level* check (`check_level`, `python -m core.mic_check --seconds 30`): a
few seconds of speech through the take's own open path, peak/RMS, and plain words about the
Windows mic level or the mic's gain.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .log_setup import get_logger

log = get_logger("capture")

CONSENT_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
SETTINGS_URI = "ms-settings:privacy-microphone"
SOUND_SETTINGS_URI = "ms-settings:sound"
LISTEN_S = 0.5
SR = 48000

_WHERE = ("Open Start → Settings → Privacy & security → Microphone "
          "(on Windows 10: Settings → Privacy → Microphone)")

# problem id → (title, what to do)
MESSAGES: dict[str, tuple[str, str]] = {
    "device_off": (
        "Windows has the microphone switched off",
        f"{_WHERE} and turn on “Microphone access” at the top. Windows may ask for permission; "
        "say yes. Then come back and press Check again.",
    ),
    "apps_off": (
        "Windows is blocking apps from using the microphone",
        f"{_WHERE} and turn on “Let apps access your microphone”. Then press Check again.",
    ),
    "desktop_apps_off": (
        "Windows is blocking desktop apps from using the microphone",
        f"{_WHERE}, scroll down and turn on “Let desktop apps access your microphone”. "
        "Then press Check again.",
    ),
    "no_device": (
        "No microphone found",
        "Plug in your microphone (USB mics can take a few seconds to appear), then press Check again. "
        "If it still isn't found, try another USB port.",
    ),
    "open_failed": (
        "The microphone couldn't be opened",
        "Another program may be using it. Close apps like Zoom, Teams, Discord or Riverside, "
        "unplug the mic and plug it back in, then press Check again.",
    ),
    "silent": (
        "The microphone isn't sending any sound",
        f"Windows may be blocking it: {_WHERE[0].lower() + _WHERE[1:]} and make sure all three switches are on. "
        "Also check the mic isn't muted (a mute button on the mic, or Settings → System → Sound → "
        "your microphone → Volume). Then press Check again.",
    ),
}


@dataclass(frozen=True)
class MicCheckResult:
    ok: bool
    problem: str = ""                 # key of MESSAGES, "" when ok
    device: str = ""
    peak_dbfs: float | None = None
    detail: str = ""                  # technical detail for the log only

    @property
    def title(self) -> str:
        return MESSAGES[self.problem][0] if self.problem else "Your microphone works"

    @property
    def advice(self) -> str:
        return MESSAGES[self.problem][1] if self.problem else ""

    @property
    def settings_uri(self) -> str:
        return SOUND_SETTINGS_URI if self.problem in ("no_device", "open_failed") else SETTINGS_URI


# --------------------------------------------------------------------------- privacy switches

RegReader = Callable[[str, str, str], "str | None"]   # (hive "HKCU"/"HKLM", key path, value name)


def _read_registry(hive: str, path: str, name: str) -> str | None:
    if sys.platform != "win32":
        return None
    import winreg
    root = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
    try:
        with winreg.OpenKey(root, path) as k:
            value, _ = winreg.QueryValueEx(k, name)
            return str(value)
    except OSError:
        return None


def privacy_problem(read: RegReader = _read_registry) -> str:
    """'' when no switch is known to be off. Missing keys count as allowed (older Windows)."""
    def denied(hive: str, path: str) -> bool:
        return (read(hive, path, "Value") or "").strip().lower() == "deny"
    if denied("HKLM", CONSENT_KEY):
        return "device_off"
    if denied("HKCU", CONSENT_KEY):
        return "apps_off"
    if denied("HKCU", CONSENT_KEY + r"\NonPackaged"):
        return "desktop_apps_off"
    return ""


# --------------------------------------------------------------------------- listening

Listener = Callable[[], "tuple[str, np.ndarray]"]     # → (device name, samples); raises on failure


class NoInputDevice(RuntimeError):
    pass


def _listen_default() -> tuple[str, np.ndarray]:
    import sounddevice as sd
    try:
        info = sd.query_devices(kind="input")
    except Exception as e:                       # PortAudio: "no default input device"
        raise NoInputDevice(str(e)) from e
    name = str(info.get("name", "")) if isinstance(info, dict) else ""
    rate = SR
    try:
        sd.check_input_settings(channels=1, samplerate=SR, dtype="float32")
    except Exception:
        rate = int(info.get("default_samplerate") or SR)
    data = sd.rec(int(LISTEN_S * rate), samplerate=rate, channels=1, dtype="float32", blocking=True)
    return name, np.asarray(data, dtype=np.float32).reshape(-1)


def classify_samples(x: np.ndarray) -> tuple[bool, float | None]:
    """(hears something, peak dBFS). Exact zeros = blocked or muted; any room noise = working."""
    x = np.asarray(x, dtype=np.float32)
    if x.size == 0:
        return False, None
    # Skip the first 50 ms: some drivers start with a few zero buffers.
    body = x[min(x.size - 1, int(0.05 * SR)):]
    peak = float(np.max(np.abs(body))) if body.size else 0.0
    if peak == 0.0:
        return False, None
    return True, float(20.0 * np.log10(peak))


def check_microphone(read: RegReader = _read_registry, listen: Listener = _listen_default) -> MicCheckResult:
    blocked = privacy_problem(read)
    if blocked:
        return MicCheckResult(ok=False, problem=blocked, detail="privacy switch off")
    try:
        device, samples = listen()
    except NoInputDevice as e:
        return MicCheckResult(ok=False, problem="no_device", detail=str(e))
    except Exception as e:                       # PortAudio errors (device busy, unplugged…)
        return MicCheckResult(ok=False, problem="open_failed", detail=f"{type(e).__name__}: {e}")
    hears, peak = classify_samples(samples)
    if not hears:
        return MicCheckResult(ok=False, problem="silent", device=device, detail="only digital zeros")
    return MicCheckResult(ok=True, device=device, peak_dbfs=peak)


# =========================================================================== level check (T30)
#
# "Is my voice at a good level on this mic?" Records a few seconds of speech through exactly the
# path a take uses (MicSource: the mic's profile, WASAPI exclusive → shared → other host APIs →
# default), measures peak and voice RMS at the mic's own level (before Hush's calibrated gain), and
# says in plain words what to change: the Windows mic level, the mic's/interface's gain knob, or
# the distance. The tips come from the mic's profile (Q2U: Windows level to 100; NT1: clipping and
# room noise; Fifine: level and gain knob).

LEVEL_CHECK_S = 5.0
LEVEL_QUIET_DB = -22.0      # sentence peak below this needs > 12 dB boost (calibration.STRONG_BOOST_DB)
LEVEL_LOUD_DB = -2.0        # peaks this hot are one loud word away from clipping (calibration.LIVE_LOUD_DB)
LEVEL_MIN_VOICE_S = 1.0

LEVEL_TITLES = {
    "silent": "The microphone isn't sending any sound",
    "no_speech": "We couldn't hear you speaking",
    "quiet": "Your voice is too quiet",
    "good": "Your level is good",
    "loud": "Your voice is too loud",
}


@dataclass(frozen=True)
class LevelCheck:
    verdict: str                      # silent | no_speech | quiet | good | loud
    title: str
    advice: str                       # plain words for Sam
    mic: str = "Generic"              # profile name
    profile_id: str = "generic"
    device: str = ""
    path: str = ""                    # how it opened, e.g. "WASAPI exclusive (direct from the mic)"
    peak_dbfs: float = -120.0         # sample peak, at the mic's own level
    rms_dbfs: float = -120.0          # RMS of the voiced 100 ms blocks
    floor_dbfs: float = -120.0        # room between words (10th percentile of 100 ms blocks)
    voice_s: float = 0.0
    clipped: int = 0
    overflows: int = 0                # input buffers the driver lost (should be 0)
    noisy_room: bool = False

    @property
    def ok(self) -> bool:
        return self.verdict == "good" and not self.overflows

    def summary(self) -> str:
        """One line for the log / the manual check."""
        return (f"{self.mic} via {self.path or '?'}: peak {self.peak_dbfs:.1f} dBFS, voice RMS "
                f"{self.rms_dbfs:.1f} dBFS, room {self.floor_dbfs:.1f} dBFS, voice {self.voice_s:.1f} s, "
                f"clipped {self.clipped}, overflows {self.overflows} -> {self.verdict}")


def level_verdict(x: np.ndarray, profile=None, *, device: str = "", path: str = "", overflows: int = 0) -> LevelCheck:
    """Plain-language verdict on a few seconds of speech `x` (mono 48 kHz float, the mic's own level)."""
    from .calibration import MIN_SNR_DB, SPEECH_OVER_FLOOR_DB, block_levels, measure_speech
    from .mic_profiles import GENERIC
    p = profile or GENERIC
    x = np.asarray(x, dtype=np.float32).reshape(-1)[int(0.05 * SR):]   # skip driver start-up
    common = dict(mic=p.name, profile_id=p.id, device=device, path=path, overflows=overflows)
    if not x.size or not np.any(x):
        return LevelCheck("silent", LEVEL_TITLES["silent"], MESSAGES["silent"][1], **common)
    rms, _ = block_levels(x)
    floor = float(np.percentile(rms, 10))
    sp = measure_speech(x, floor)
    m = dict(peak_dbfs=sp.peak_db, rms_dbfs=sp.rms_db, floor_dbfs=round(floor, 1), voice_s=sp.active_s,
             clipped=sp.clipped, **common)
    if sp.active_s < LEVEL_MIN_VOICE_S or sp.rms_db < floor + SPEECH_OVER_FLOOR_DB:
        who = device or "your microphone" if p.is_generic else f"the {p.name}"
        return LevelCheck("no_speech", LEVEL_TITLES["no_speech"],
                          f"Speak at your normal recording voice for the whole check, into {who}, and make "
                          "sure it isn't muted. Then check again.", **m)
    snr = sp.rms_db - floor
    if sp.clipped or sp.peak_db >= LEVEL_LOUD_DB:
        lead = ("Your voice is clipping, which sounds harsh and can't be fixed afterwards. " if sp.clipped
                else "One louder word would clip. ")
        verdict, tip = "loud", lead + p.loud_tip
    elif sp.peak_db < LEVEL_QUIET_DB:
        verdict, tip = "quiet", p.quiet_tip
    else:
        verdict, tip = "good", ""
    parts = [tip] if tip else []
    if snr < MIN_SNR_DB:
        parts.append(f"Your voice is only {snr:.0f} dB above the room. {p.room_tip}")
    if overflows:
        parts.append("Some sound was lost while checking: close other programs that use the microphone "
                     "(Zoom, Teams, Riverside) and check again.")
    if not parts:
        parts.append("Nothing to change: record at this distance and with this voice.")
    return LevelCheck(verdict, LEVEL_TITLES[verdict], " ".join(parts), noisy_room=snr < MIN_SNR_DB, **m)


Recording = Callable[[float], "tuple[np.ndarray, dict, object, int]"]   # seconds -> (samples, info, profile, overflows)


def _record_via_micsource(seconds: float, device: int | str | None = None,
                          profile: str | None = None) -> tuple[np.ndarray, dict, object, int]:
    """Record through MicSource: the same profile and open path as a real take."""
    import threading
    from .capture import MicSource
    src = MicSource(device, profile)
    blocks: list[np.ndarray] = []
    want = int(seconds * SR)
    got = [0]
    done = threading.Event()

    def on_block(b):                                  # audio callback: append only
        if got[0] < want:
            blocks.append(b)
            got[0] += len(b)
            if got[0] >= want:
                done.set()

    src.start(on_block)
    try:
        if not done.wait(seconds + 5.0):
            log.warning("level check: only %.1f of %.1f s arrived", got[0] / SR, seconds)
    finally:
        src.stop()
    x = np.concatenate(blocks)[:want] if blocks else np.zeros(0, np.float32)
    return x, dict(src.info), src.profile, src.overflows


def check_level(seconds: float = LEVEL_CHECK_S, device: int | str | None = None, profile: str | None = None,
                record: Recording | None = None) -> LevelCheck:
    """Record `seconds` of speech on the mic Hush would use and give a plain-language verdict.
    Open failures raise (the caller shows MESSAGES['open_failed'])."""
    rec = record or (lambda s: _record_via_micsource(s, device, profile))
    x, info, prof, overflows = rec(seconds)
    res = level_verdict(x, prof, device=str(info.get("device", "")), path=str(info.get("mode", "")),
                        overflows=int(overflows))
    log.info("level check: %s [%s]", res.summary(), info.get("host_api", "?"))
    return res


def main(argv: list[str] | None = None) -> int:
    """Manual check (T30): `python -m core.mic_check [--seconds 30] [--profile id] [--device name]`.
    Speak normally while it records; prints the path, levels, overflows and the verdict (also logged)."""
    args = list(sys.argv[1:] if argv is None else argv)

    def opt(name, default=None):
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default

    seconds = float(opt("--seconds", LEVEL_CHECK_S))
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")     # advice has arrows; cp1252 consoles
    except Exception:
        pass
    try:
        from .log_setup import setup_logging
        setup_logging()
    except Exception:
        pass
    print(f"Speak at your normal recording voice for {seconds:.0f} s...")
    try:
        res = check_level(seconds, opt("--device"), opt("--profile"))
    except Exception as e:                       # no mic, or every way of opening it failed
        log.warning("level check: could not open the microphone: %s: %s", type(e).__name__, e)
        print(f"\n{MESSAGES['open_failed'][0]}\n{MESSAGES['open_failed'][1]}\n({type(e).__name__}: {e})")
        return 2
    print(res.summary())
    print(f"\n{res.title}\n{res.advice}")
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
