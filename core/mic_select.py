"""Pick the best way to open the microphone, guided by its profile (T29: Q2U, Rode NT1, Fifine).

Windows exposes one USB mic through several host APIs. MME / DirectSound / WASAPI-shared go
through the Windows audio engine (resampling, optional auto-gain and "enhancements"). WASAPI
*exclusive* talks to the device directly, so what the mic sends is what we record. Exclusive blocks
other apps from the mic while recording, so we fall back to shared mode if it won't open, then to the
other host APIs; MicSource (core/capture.py) finally falls back to the system default.

Auto (`device=None`): `mic_profiles.detect()` finds the known mic (priority Q2U > Rode NT1 > Fifine);
an unknown mic (Generic) returns no candidates, i.e. the system default as before.

Manual check:  python -m core.mic_select [--profile <id>]
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from .mic_profiles import GENERIC, MicProfile, clean_name, detect, get_profile, profile_for_device

API_RANK = {"Windows WASAPI": 0, "Windows WDM-KS": 1, "Windows DirectSound": 2, "MME": 3}


@dataclass(frozen=True)
class Candidate:
    device: int
    exclusive: bool
    label: str


def _same_mic(a: str, b: str) -> bool:
    a, b = clean_name(a).lower(), clean_name(b).lower()
    return bool(a and b) and (a.startswith(b) or b.startswith(a))      # MME truncates to 31 chars


def plan(devices: list[dict], hostapis: list[dict], device: int | str | None = None,
         profile: MicProfile | str | None = None) -> tuple[MicProfile, list[Candidate]]:
    """(profile, ordered ways to open it). `device` = explicit choice (index/name) or None = auto.
    `profile` = override (profile or its id/name); None = detect from the device names."""
    if isinstance(profile, str):
        profile = get_profile(profile)
    ins = [(i, d) for i, d in enumerate(devices) if d.get("max_input_channels", 0) > 0]

    def api(d):
        try:
            return hostapis[d["hostapi"]]["name"]
        except (IndexError, KeyError, TypeError):
            return ""

    explicit = device is not None and device != ""
    if isinstance(device, int):
        chosen = [(i, d) for i, d in ins if i == device]
    elif isinstance(device, str) and device:
        chosen = [(i, d) for i, d in ins if device.lower() in d["name"].lower()]
    else:
        det = detect(devices, hostapis, override=profile.id if profile else None)
        profile = det.profile
        chosen = [(i, d) for i, d in ins if det.device_name and _same_mic(d["name"], det.device_name)]
    if explicit and profile is None:
        profile = profile_for_device(chosen[0][1]["name"]) if chosen else GENERIC
    profile = profile or GENERIC
    if not chosen:
        return profile, []                         # caller falls back to the system default

    # Same physical mic as the chosen one, on every host API; the profile's best API first.
    same = [(i, d) for i, d in ins if any(_same_mic(d["name"], c["name"]) for _, c in chosen)] or chosen
    best = profile.best_host_api
    same.sort(key=lambda t: (api(t[1]) != best if best else False, API_RANK.get(api(t[1]), 9)))
    exclusive = profile.exclusive or explicit      # an explicit pick keeps the T27 behaviour
    out: list[Candidate] = []
    for i, d in same:
        if api(d) == "Windows WASAPI":
            if exclusive:
                out.append(Candidate(i, True, "WASAPI exclusive (direct from the mic)"))
            out.append(Candidate(i, False, "WASAPI shared"))
        elif api(d) in API_RANK:
            out.append(Candidate(i, False, api(d)))
    return profile, out


def candidates(devices: list[dict], hostapis: list[dict], device: int | str | None = None,
               profile: MicProfile | str | None = None) -> list[Candidate]:
    """Ordered ways to try opening the mic ([] = use the system default)."""
    return plan(devices, hostapis, device, profile)[1]


def main(argv: list[str] | None = None) -> int:
    """Print what Hush sees: every input, the detected mic, and the order it will try to open it."""
    args = list(sys.argv[1:] if argv is None else argv)
    override = args[args.index("--profile") + 1] if "--profile" in args and args.index("--profile") + 1 < len(args) else None
    try:
        import sounddevice as sd
        devices, hostapis = list(sd.query_devices()), list(sd.query_hostapis())
    except Exception as e:
        print(f"Could not list audio devices: {e}")
        devices, hostapis = [], []
    print("Inputs (all host APIs):")
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) > 0:
            api = hostapis[d["hostapi"]]["name"] if d["hostapi"] < len(hostapis) else "?"
            print(f"  [{i:>3}] {api:<20} {d['name']}  ({d['default_samplerate']:.0f} Hz)")
    det = detect(devices, hostapis, override=override)
    print("\nDistinct mics:")
    for name, pid in det.found:
        print(f"  {name:<45} -> {pid}")
    if not det.found:
        print("  (none)")
    p = det.profile
    print(f"\nDetected: {det.describe()}  [profile '{p.id}', {p.type}, {p.connection}]")
    if p.expected_speech_dbfs is not None:
        print(f"  expected sentence peak ~{p.expected_speech_dbfs:.1f} dBFS before calibration")
    if det.unmatched:
        print(f"  unknown inputs (add to config/mics.toml if real mics): {', '.join(det.unmatched)}")
    _, cands = plan(devices, hostapis, None, override)
    print("\nOpen order:")
    for c in cands:
        print(f"  [{c.device:>3}] {c.label}")
    print("  system default (last resort)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
