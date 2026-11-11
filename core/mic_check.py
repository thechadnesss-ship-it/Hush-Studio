"""First-run microphone check (T21): can Hush Studio hear the mic, and if not, why?

Windows has three privacy switches that silently block a desktop app's microphone; when one is
off the app still opens the mic but receives exact digital zeros. So the check reads those
switches (registry, no admin needed) and also listens for half a second. Every problem comes
with plain-language steps; `SETTINGS_URI` opens the right Settings page directly.

Pure logic with injectable registry/listen functions, so it is testable without hardware.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable

import numpy as np

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
